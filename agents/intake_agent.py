"""Intake agent for collecting customer profile information and finalizing bookings.

Replaced TaskGroup with @function_tool methods so the LLM always operates under
IntakeAgent's full system prompt (with business facts) and has access to all tools
(profile collection + return_to_frontdesk) at all times.
"""
import hashlib
import logging

from livekit.agents.llm import function_tool

from .base_agent import BaseAgent, RunContext_T
from tasks.dog_weight_task import derive_dog_size_from_weight
from tasks.phone_task import validate_phone
from tools.availability_provider import compute_selection_quote, get_service_display_label
from tools.handoff_email_tools import build_handoff_payload, send_handoff_email
from utils import (
    ensure_session_trace_id,
    load_prompt,
    trace_log,
    userdata_diff,
    userdata_snapshot,
)

logger = logging.getLogger("doheny-surf-desk.intake")


class IntakeAgent(BaseAgent):
    """Agent responsible for collecting customer profile information and finalizing bookings."""

    def __init__(self, chat_ctx=None):
        # No per-agent TTS override: Intake inherits SESSION_TTS so all speech
        # (agent say + LLM replies) uses the same voice consistently.
        agent_kwargs = {
            "instructions": load_prompt(
                'intake_prompt.yaml',
                include_business_facts=True,
            ),
            "chat_ctx": chat_ctx,
        }
        super().__init__(**agent_kwargs)

    async def on_enter(self) -> None:
        """Greet the caller and prompt for the first piece of profile info."""
        await self.session.say(
            "Welcome to the Intake Department at Happy Hound. "
            "I'll just need a few quick details to finalize your booking."
        )
        await self.session.generate_reply(
            instructions=(
                "You have already greeted the caller — do not repeat the greeting. "
                "Ask for their full name to get started."
            )
        )

    # ------------------------------------------------------------------
    # Profile collection tools
    # ------------------------------------------------------------------

    @function_tool
    async def record_name(
        self,
        context: RunContext_T,
        name: str,
    ) -> str:
        """Record the customer's full name.

        Args:
            context: RunContext with userdata
            name: The customer's full name
        """
        userdata = context.userdata
        userdata.name = name
        return (
            f"NAME_RECORDED: {name}. "
            "Now ask for the caller's phone number."
        )

    @function_tool
    async def record_phone(
        self,
        context: RunContext_T,
        phone: str,
    ) -> str:
        """Record and validate the customer's phone number.

        Args:
            context: RunContext with userdata
            phone: The phone number provided by the caller
        """
        if not validate_phone(phone):
            return (
                "PHONE_INVALID: The phone number must have at least 10 digits. "
                "Ask the caller to repeat their phone number."
            )
        userdata = context.userdata
        userdata.phone = phone
        return (
            f"PHONE_RECORDED: {phone}. "
            "Read the phone number back to the caller and ask them to confirm it is correct."
        )

    @function_tool
    async def confirm_phone(self, context: RunContext_T) -> str:
        """Confirm the phone number after the caller has verified the read-back."""
        userdata = context.userdata
        if not userdata.phone:
            return "ERROR: No phone number recorded yet. Call record_phone first."
        return (
            f"PHONE_CONFIRMED: {userdata.phone}. "
            "Now ask for the dog's current weight in pounds."
        )

    @function_tool
    async def record_dog_weight(
        self,
        context: RunContext_T,
        weight_lbs: float,
    ) -> str:
        """Record the dog's weight and derive the size tier.

        Args:
            context: RunContext with userdata
            weight_lbs: The dog's weight in pounds
        """
        if weight_lbs < 2 or weight_lbs > 300:
            return (
                "WEIGHT_INVALID: Weight must be between 2 and 300 pounds. "
                "Ask the caller to confirm the weight."
            )
        dog_size = derive_dog_size_from_weight(weight_lbs)
        userdata = context.userdata
        userdata.dog_weight_lbs = weight_lbs
        userdata.dog_size = dog_size
        return (
            f"WEIGHT_RECORDED: {weight_lbs} lbs ({dog_size} size tier). "
            f"Read back '{weight_lbs} pounds, which maps to our {dog_size} size tier' "
            "and ask the caller to confirm."
        )

    @function_tool
    async def confirm_dog_weight(self, context: RunContext_T) -> str:
        """Confirm the dog weight after the caller has verified the read-back.

        If all profile fields are now complete, this automatically finalizes
        the booking (computes quote, generates booking ID, sends handoff email).
        """
        userdata = context.userdata
        if not userdata.dog_weight_lbs:
            return "ERROR: No dog weight recorded yet. Call record_dog_weight first."

        # Check if profile is complete
        if not (userdata.name and userdata.phone and userdata.dog_weight_lbs):
            missing = []
            if not userdata.name:
                missing.append("name")
            if not userdata.phone:
                missing.append("phone")
            return (
                f"WEIGHT_CONFIRMED but profile incomplete — still need: {', '.join(missing)}. "
                "Continue collecting the missing details."
            )

        # Profile complete — finalize booking
        return await self._finalize_booking(context)

    # ------------------------------------------------------------------
    # Transfer tool
    # ------------------------------------------------------------------

    @function_tool
    async def return_to_frontdesk(self, context: RunContext_T) -> BaseAgent:
        """Return customer to front desk for additional help or to start a new service."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="intake.return_to_frontdesk",
            from_agent="IntakeAgent",
            to_agent="FrontDeskAgent",
            summary=userdata.summarize(),
        )
        await self.session.say(
            "Sure. I'm now transferring your call back to our Front Desk Department. "
            "Kindly wait a moment while I connect you."
        )
        from agents.frontdesk_agent import FrontDeskAgent
        return FrontDeskAgent(chat_ctx=self.chat_ctx)

    # ------------------------------------------------------------------
    # Booking finalization (private)
    # ------------------------------------------------------------------

    async def _finalize_booking(self, context: RunContext_T) -> str:
        """Compute quote, generate booking ID, send handoff email, speak confirmation."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        before = userdata_snapshot(userdata)

        userdata.selection_source = userdata.selection_source or "intake.tools"
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
            f"You're all set! Your {service_label} is confirmed for {booking_date} at {booking_time}. "
            f"Your reference number is {booking_id}. "
            "Our team will be in touch shortly to confirm all the details. "
            "Is there anything else I can help you with today?"
        )

        return (
            f"BOOKING_FINALIZED: Booking ID {booking_id}, {service_label} on {booking_date} at {booking_time}. "
            f"Total: ${float(quote['total']):.2f}. Email {'sent' if userdata.handoff_status == 'sent' else 'pending'}. "
            "The confirmation has been spoken to the caller. If they have more questions, answer from the business facts. "
            "If they want to book another service, call return_to_frontdesk()."
        )
