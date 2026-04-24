"""Intake agent for collecting customer profile information and finalizing bookings.

Replaced TaskGroup with @function_tool methods so the LLM always operates under
IntakeAgent's full system prompt (with business facts) and has access to all tools
(profile collection + return_to_frontdesk) at all times.
"""
import hashlib
import logging

from livekit.agents.llm import function_tool

from .base_agent import BaseAgent, RunContext_T, reset_booking_state
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
        """Greet the caller and either read back the booking (if profile is
        already on file) or start collecting profile info.

        Finalization NEVER happens automatically on entry. The caller must
        verbally confirm the read-back first (via the confirm_booking_details
        tool) — this gives them a chance to catch wrong service / wrong time
        BEFORE the email is sent.
        """
        userdata = self.session.userdata
        profile_complete = bool(
            userdata.name and userdata.phone and userdata.dog_weight_lbs
        )

        if profile_complete:
            # On re-entry, DO NOT silently reuse the profile. The caller might
            # be booking for a second dog, or with different contact info.
            # Speak the on-file profile back and ask if it applies.
            weight = int(userdata.dog_weight_lbs or 0)
            size = (userdata.dog_size or "").replace("x-large", "extra-large")
            size_clause = f" ({size})" if size else ""
            phone_display = userdata.phone or "not on file"

            await self.session.say(
                f"Welcome back to the Intake Department. I have you on file as "
                f"{userdata.name}, phone {phone_display}, with a {weight}-pound "
                f"dog{size_clause}. Is this booking for the same dog and contact "
                "info, or does anything need to change?"
            )
            await self.session.generate_reply(
                instructions=(
                    "Wait for the caller's answer about whether the profile on file "
                    "still applies to THIS booking. "
                    "- If they say it's the same / correct / yes, call _speak_readback "
                    "indirectly by proceeding: your next step is to inform them you "
                    "will now confirm the booking details, then the system will "
                    "read them back automatically. To trigger this, call "
                    "proceed_to_readback(). "
                    "- If they say the dog's weight is different, call "
                    "update_dog_weight(weight_lbs=<new>). "
                    "- If they say the phone number is different, call "
                    "update_phone(phone=<new>). "
                    "- If they say the name is different, call "
                    "update_customer_name(name=<new>). "
                    "After any update tool, the system re-reads the updated profile "
                    "and you continue the flow. Never assume the caller has said "
                    "'same' — they must say so explicitly before you call "
                    "proceed_to_readback."
                )
            )
            return

        await self.session.say(
            "Welcome to the Intake Department at Happy Hound. "
            "I'll just need a few quick details to finalize your booking."
        )

        missing_parts = []
        if not userdata.name:
            missing_parts.append("name")
        if not userdata.phone:
            missing_parts.append("phone number")
        if not userdata.dog_weight_lbs:
            missing_parts.append("dog's weight")

        await self.session.generate_reply(
            instructions=(
                "You have already greeted the caller — do not repeat the greeting. "
                f"Ask for the first missing detail: {missing_parts[0]}. "
                "Do not list all the missing fields in one turn — ask one at a time."
            )
        )

    async def _speak_readback(self) -> None:
        """Speak the current booking details back to the caller before finalizing.

        For grooming (where price depends on dog size), includes the dog's
        weight and size tier so the caller can catch stale profile data.
        For other services (flat pricing), dog size is omitted since it
        doesn't affect the price and only adds clutter.
        """
        userdata = self.session.userdata
        slot = userdata.runtime_tool_facts.get("confirmed_slot", {})
        service_family = slot.get("service_family") or userdata.service_family or "daycare"
        service_plan = slot.get("service_plan") or userdata.service_plan
        service_label = slot.get("service_label") or get_service_display_label(service_family, service_plan)
        booking_date = slot.get("date") or userdata.requested_date or "today"
        booking_time = slot.get("time") or userdata.requested_time or ""

        quote = compute_selection_quote(
            service_family=service_family,
            service_plan=service_plan,
            dog_size=userdata.dog_size,
        )
        total_str = f"${float(quote['total']):.2f}"

        time_clause = f" at {booking_time}" if booking_time else ""

        # For grooming, the dog's weight drives the price — include it in the
        # read-back so the caller can catch stale weight from a prior booking.
        dog_clause = ""
        if service_family == "grooming" and userdata.dog_weight_lbs:
            weight = int(userdata.dog_weight_lbs)
            size = (userdata.dog_size or "").replace("x-large", "extra-large")
            dog_clause = f" for a {weight}-pound {size} dog" if size else f" for a {weight}-pound dog"

        await self.session.say(
            f"Let me read your booking back to make sure it's correct: "
            f"{service_label}{dog_clause} on {booking_date}{time_clause}, total {total_str}. "
            "Is that right?"
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

        Does NOT finalize the booking. Instead, reads the full booking details
        (service, date, time, price) back to the caller and waits for them to
        verbally confirm before any email is sent. The LLM must then call
        confirm_booking_details() on explicit caller confirmation, or
        correct_booking() if the caller says something is wrong.
        """
        userdata = context.userdata
        if not userdata.dog_weight_lbs:
            return "ERROR: No dog weight recorded yet. Call record_dog_weight first."

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

        # Profile complete — speak the read-back and wait for caller verification.
        await self._speak_readback()
        return (
            "WEIGHT_CONFIRMED, PROFILE_COMPLETE, READBACK_SPOKEN. "
            "You have read the booking details to the caller. Wait for their answer. "
            "If they confirm it is correct, call confirm_booking_details() to finalize. "
            "If they say something is wrong (wrong service, wrong date, wrong time), "
            "call correct_booking() to transfer back to Scheduling."
        )

    @function_tool
    async def confirm_booking_details(self, context: RunContext_T) -> str:
        """Finalize the booking AFTER the caller has verbally confirmed the read-back.

        Call ONLY when the caller has explicitly said yes to the service, date,
        time, and price you just read back. This triggers quote computation,
        booking ID generation, and the SMTP handoff email.
        """
        return await self._finalize_booking(context)

    @function_tool
    async def correct_booking(self, context: RunContext_T) -> BaseAgent:
        """Transfer to Scheduling when the caller says the read-back is wrong.

        Clears the current booking state (service, date, time, confirmed slot)
        so Scheduler can re-select. Customer profile (name/phone/weight) is
        preserved — caller won't have to give those again. Scheduler's on_enter
        will ask the caller what they actually want, so no description of the
        correction is needed at this layer.
        """
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        reset_booking_state(userdata)
        userdata.runtime_tool_facts["reentry_target"] = "scheduler"
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="intake.correct_booking",
            from_agent="IntakeAgent",
            to_agent="SchedulerAgent",
            summary=userdata.summarize(),
        )
        await self.session.say(
            f"I'm sorry about that. I'm transferring you back to our Scheduling "
            f"Department to fix it. Kindly wait a moment."
        )
        from agents.scheduler_agent import SchedulerAgent
        return SchedulerAgent(chat_ctx=self.chat_ctx)

    # ------------------------------------------------------------------
    # Inline profile-update tools (used during read-back / re-entry)
    # ------------------------------------------------------------------

    @function_tool
    async def update_dog_weight(
        self,
        context: RunContext_T,
        weight_lbs: float,
    ) -> str:
        """Update the dog's weight on file and re-read the booking back.

        Use when the caller says the dog's weight on file is wrong — e.g.
        "this booking is for a different dog, 110 pounds" or "actually my
        dog is 90 pounds". This updates userdata.dog_weight_lbs and the
        derived size tier, then speaks the updated booking (with the new
        price) back to the caller.

        Args:
            context: RunContext with userdata
            weight_lbs: The dog's weight in pounds (2-300)
        """
        if weight_lbs < 2 or weight_lbs > 300:
            return (
                "WEIGHT_INVALID: Weight must be between 2 and 300 pounds. "
                "Ask the caller to confirm the weight."
            )
        userdata = context.userdata
        old_weight = userdata.dog_weight_lbs
        userdata.dog_weight_lbs = weight_lbs
        userdata.dog_size = derive_dog_size_from_weight(weight_lbs)
        # Re-speak the updated booking so caller hears the new price
        await self._speak_readback()
        return (
            f"DOG_WEIGHT_UPDATED: was {old_weight}, now {weight_lbs} lbs "
            f"({userdata.dog_size}). The read-back has been spoken again with "
            "the new price. Wait for the caller's answer. If they say yes, "
            "call confirm_booking_details. If they want to change something "
            "else, call the matching update tool."
        )

    @function_tool
    async def update_phone(
        self,
        context: RunContext_T,
        phone: str,
    ) -> str:
        """Update the phone number on file and re-read the booking back."""
        if not validate_phone(phone):
            return (
                "PHONE_INVALID: The phone number must have at least 10 digits. "
                "Ask the caller to repeat their phone number."
            )
        userdata = context.userdata
        userdata.phone = phone
        await self._speak_readback()
        return (
            f"PHONE_UPDATED: now {phone}. The read-back has been spoken again. "
            "Wait for the caller's answer."
        )

    @function_tool
    async def update_customer_name(
        self,
        context: RunContext_T,
        name: str,
    ) -> str:
        """Update the customer name on file and re-read the booking back."""
        userdata = context.userdata
        userdata.name = name
        await self._speak_readback()
        return (
            f"NAME_UPDATED: now {name}. The read-back has been spoken again. "
            "Wait for the caller's answer."
        )

    @function_tool
    async def proceed_to_readback(self, context: RunContext_T) -> str:
        """Speak the booking read-back to the caller.

        Use this on re-entry ONLY after the caller has explicitly confirmed
        that the profile on file (name, phone, dog) still applies to this
        booking. Do not call it before the caller says "same" / "yes" /
        "correct" to the profile-applicability question.
        """
        await self._speak_readback()
        return (
            "READBACK_SPOKEN. Wait for the caller's answer. If they confirm, "
            "call confirm_booking_details. If they say something is wrong, "
            "call the matching update tool (update_dog_weight, update_phone, "
            "update_customer_name) or correct_booking for service/date/time issues."
        )

    # ------------------------------------------------------------------
    # Transfer tool
    # ------------------------------------------------------------------

    @function_tool
    async def return_to_frontdesk(self, context: RunContext_T) -> BaseAgent:
        """Return customer to front desk for general help or to cancel entirely.

        Clears the current booking state so Front Desk starts fresh.
        """
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        reset_booking_state(userdata)
        userdata.runtime_tool_facts["reentry_target"] = "frontdesk"
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

    @function_tool
    async def return_to_scheduler(self, context: RunContext_T) -> BaseAgent:
        """Transfer to Scheduling to check availability for a DIFFERENT service.

        Use when the caller wants to change their booking mid-intake. Clears
        the current booking state (service, date, time, confirmed slot) so
        Scheduler starts fresh, but keeps customer profile (name, phone,
        dog weight) intact so they don't have to give those details again.
        """
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        reset_booking_state(userdata)
        userdata.runtime_tool_facts["reentry_target"] = "scheduler"
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="intake.return_to_scheduler",
            from_agent="IntakeAgent",
            to_agent="SchedulerAgent",
            summary=userdata.summarize(),
        )
        await self.session.say(
            "Sure. I'm now transferring you back to our Scheduling Department "
            "to check availability for a different service. Kindly wait a moment."
        )
        from agents.scheduler_agent import SchedulerAgent
        return SchedulerAgent(chat_ctx=self.chat_ctx)

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
