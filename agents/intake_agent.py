"""Intake agent for collecting customer profile information and finalizing bookings."""
import hashlib
import logging

from livekit.agents.beta.workflows import TaskGroup

from .base_agent import BaseAgent
from tasks.dog_weight_task import DogWeightTask
from tasks.name_task import NameTask
from tasks.phone_task import PhoneTask
from tools.availability_provider import compute_selection_quote, get_service_display_label
from tools.handoff_email_tools import build_handoff_payload, send_handoff_email
from utils import (
    ensure_session_trace_id,
    load_prompt,
    resolve_agent_tts,
    trace_log,
    userdata_diff,
    userdata_snapshot,
)

# Legacy intake tasks retained for future use:
# from tasks.age_task import AgeTask
# from tasks.email_task import GetEmailTask
# from tasks.experience_task import ExperienceTask

logger = logging.getLogger("doheny-surf-desk.intake")


class IntakeAgent(BaseAgent):
    """Agent responsible for collecting customer profile information and finalizing bookings."""

    def __init__(self, chat_ctx=None):
        agent_kwargs = {
            "instructions": load_prompt(
                'intake_prompt.yaml',
                include_business_facts=True,
            ),
            "chat_ctx": chat_ctx,
        }
        tts_descriptor = resolve_agent_tts("intake")
        if tts_descriptor:
            agent_kwargs["tts"] = tts_descriptor
        super().__init__(**agent_kwargs)
    
    async def on_enter(self) -> None:
        """Collect customer profile then finalize the booking confirmed by Scheduler."""
        await self.session.say(
            "Almost there! I just need a few quick details to lock in your booking."
        )

        # Create TaskGroup for sequential profile collection
        task_group = TaskGroup()

        task_group.add(
            lambda: NameTask(),
            id="name_task",
            description="Collects customer's full name"
        )

        task_group.add(
            lambda: PhoneTask(),
            id="phone_task",
            description="Collects phone number with confirmation"
        )

        task_group.add(
            lambda: DogWeightTask(),
            id="dog_weight_task",
            description="Collects dog weight and size tier"
        )

        # Execute all tasks sequentially
        results = await task_group
        task_results = results.task_results

        # Update userdata from task results
        userdata = self.session.userdata
        trace_id = ensure_session_trace_id(userdata)
        before = userdata_snapshot(userdata)
        userdata.name = task_results["name_task"].name
        userdata.phone = task_results["phone_task"].phone
        userdata.dog_weight_lbs = task_results["dog_weight_task"].weight_lbs
        userdata.dog_size = task_results["dog_weight_task"].dog_size
        userdata.selection_source = userdata.selection_source or "intake.tasks"
        userdata.runtime_tool_facts["intake_profile"] = {
            "name": userdata.name,
            "phone": userdata.phone,
            "dog_weight_lbs": userdata.dog_weight_lbs,
            "dog_size": userdata.dog_size,
        }

        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="intake.profile_collected",
            from_agent="IntakeAgent",
            changes=userdata_diff(before, userdata_snapshot(userdata)),
        )

        # --- Finalize booking ---
        confirmed_slot = userdata.runtime_tool_facts.get("confirmed_slot", {})
        service_family = confirmed_slot.get("service_family") or userdata.service_family or "daycare"
        service_plan = confirmed_slot.get("service_plan") or userdata.service_plan
        service_label = confirmed_slot.get("service_label") or get_service_display_label(service_family, service_plan)
        booking_date = confirmed_slot.get("date") or userdata.requested_date or "today"
        booking_time = confirmed_slot.get("time") or userdata.requested_time or ""
        staff = confirmed_slot.get("staff") or userdata.instructor_name or "Happy Hound Team"
        duration_str = confirmed_slot.get("duration") or ""

        quote = compute_selection_quote(
            service_family=service_family,
            service_plan=service_plan,
            dog_size=userdata.dog_size,
        )

        booking_seed = hashlib.sha256(
            f"{userdata.name}|{service_family}|{service_plan}|{booking_date}|{booking_time}".encode("utf-8")
        ).hexdigest()[:8].upper()
        booking_id = f"HH-{booking_seed}"

        userdata.booking_id = booking_id
        userdata.instructor_name = staff
        userdata.service_family = service_family
        userdata.service_plan = service_plan
        userdata.requested_services = [service_family]
        userdata.requested_date = booking_date
        userdata.requested_time = booking_time
        userdata.quoted_subtotal = float(quote["subtotal"])
        userdata.quoted_tax = float(quote["tax"])
        userdata.quoted_total = float(quote["total"])
        userdata.total_amount = float(quote["total"])
        userdata.quote_notes = (
            f"{service_label} at {booking_time} ({duration_str}). {quote['quote_notes']}"
            if duration_str
            else f"{service_label} at {booking_time}. {quote['quote_notes']}"
        )
        userdata.handoff_status = "pending"
        userdata.preferred_date = booking_date
        userdata.preferred_time = booking_time

        userdata.runtime_tool_facts["booking"] = {
            "booking_id": booking_id,
            "service_family": service_family,
            "service_plan": service_plan,
            "service_label": service_label,
            "date": booking_date,
            "time": booking_time,
            "staff": staff,
            "duration": duration_str,
            "quote": quote,
        }
        userdata.runtime_tool_facts["quote_basis"] = {
            "service_family": service_family,
            "service_plan": service_plan,
            "dog_size": userdata.dog_size,
        }
        userdata.runtime_tool_facts["active_quote"] = quote

        payload = build_handoff_payload(userdata)
        try:
            result = send_handoff_email(payload)
            userdata.handoff_status = "sent"
            userdata.payment_status = "pending_human_followup"
            userdata.runtime_tool_facts["handoff_email"] = {
                "message_id": result["message_id"],
                "subject": result["subject"],
            }
            trace_log(
                logger=logger,
                flag_name="HH_TRACE_STATE",
                trace_id=trace_id,
                message="intake.finalize.handoff_sent",
                booking_id=booking_id,
                subject=result["subject"],
                changes=userdata_diff(before, userdata_snapshot(userdata)),
            )
            await self.session.say(
                f"You're all set! Your {service_label} is confirmed for {booking_date} at {booking_time}. "
                f"Your reference number is {booking_id}. "
                "Our team will be in touch shortly to confirm all the details. "
                "Is there anything else I can help you with today?"
            )
        except (ValueError, RuntimeError) as exc:
            userdata.handoff_status = "failed"
            logger.warning("intake.finalize.handoff_error: %r", exc, exc_info=True)
            trace_log(
                logger=logger,
                flag_name="HH_TRACE_STATE",
                trace_id=trace_id,
                message="intake.finalize.handoff_error",
                error=str(exc),
                changes=userdata_diff(before, userdata_snapshot(userdata)),
            )
            await self.session.say(
                f"Your booking details are all set. Your reference number is {booking_id}. "
                "Our team will be in touch shortly to confirm everything. "
                "Is there anything else I can help you with today?"
            )

