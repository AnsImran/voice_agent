"""Scheduler agent for Happy Hound service availability and booking."""
from __future__ import annotations

import hashlib
import logging

from livekit.agents.llm import function_tool

from .base_agent import BaseAgent, RunContext_T
from tools.availability_provider import (
    AvailabilityProvider,
    MockAvailabilityProvider,
    compute_selection_quote,
    get_service_display_label,
    resolve_service_selection,
)
from utils import (
    ensure_session_trace_id,
    get_current_date,
    load_prompt,
    resolve_agent_tts,
    trace_log,
    userdata_diff,
    userdata_snapshot,
)

logger = logging.getLogger("doheny-surf-desk.scheduler")


def _normalize_time_token(value: str) -> str:
    return value.replace(" ", "").lower()


class SchedulerAgent(BaseAgent):
    """Agent responsible for checking and confirming service availability."""

    def __init__(
        self,
        chat_ctx=None,
        provider: AvailabilityProvider | None = None,
    ):
        agent_kwargs = {
            "instructions": load_prompt(
                "scheduler_prompt.yaml",
                current_date=get_current_date(),
            ),
            "chat_ctx": chat_ctx,
        }
        tts_descriptor = resolve_agent_tts("scheduler")
        if tts_descriptor:
            agent_kwargs["tts"] = tts_descriptor
        super().__init__(**agent_kwargs)
        self.provider = provider or MockAvailabilityProvider()
        self._availability_inflight_signatures: set[str] = set()

    async def on_enter(self) -> None:
        """Auto-resume flow after handoff from intake without waiting for user speech."""
        userdata = self.session.userdata
        trace_id = ensure_session_trace_id(userdata)
        pending = getattr(userdata, "handoff_pending_action", None)
        userdata.handoff_pending_action = None

        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="scheduler.on_enter",
            pending_action=pending,
            summary=userdata.summarize(),
        )

        missing = []
        if not (userdata.service_family or userdata.requested_services):
            missing.append("service")
        if not userdata.requested_date:
            missing.append("date")
        if not userdata.requested_time:
            missing.append("time preference")

        if missing:
            await self.session.generate_reply(
                instructions=(
                    "You are now the SchedulerAgent. Start with one short department intro "
                    "exactly once, then continue immediately without waiting for additional "
                    "user prompts. Ask only for the missing details: "
                    f"{', '.join(missing)}. "
                    "If the caller previously selected Golden Leash Club Card, preserve that plan. "
                    "Do not repeat the intro in the same turn."
                )
            )
            return

        await self.session.generate_reply(
            instructions=(
                "Start with one short department intro exactly once, then briefly confirm "
                "the known service and schedule preferences and immediately call check_availability. "
                "Do not repeat the intro in the same turn."
            )
        )

    def _resolve_selection(self, userdata, selection_value: str | None) -> tuple[str, str | None]:
        existing_family = userdata.service_family or (
            userdata.requested_services[0] if userdata.requested_services else None
        )
        family, plan = resolve_service_selection(
            selection_value,
            existing_family=existing_family,
            existing_plan=userdata.service_plan,
        )
        return family, plan

    def _get_slots(
        self,
        context: RunContext_T,
        service_family: str,
        date: str,
        time_preference: str,
    ) -> list[dict]:
        slots = self.provider.get_slots(
            service=service_family,
            date=date,
            time_preference=time_preference,
            dog_size=context.userdata.dog_size,
        )
        return [slot.__dict__.copy() for slot in slots]

    @function_tool
    async def check_availability(
        self,
        context: RunContext_T,
        service: str | None = None,
        service_plan: str | None = None,
        date: str | None = None,
        time_preference: str | None = None,
        ) -> str:
        """Check service availability and return only a concise list of times."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        before = userdata_snapshot(userdata)
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_TOOLS",
            trace_id=trace_id,
            message="tool.check_availability.call",
            service=service,
            service_plan=service_plan,
            date=date,
            time_preference=time_preference,
        )

        selection_value = service_plan or service or userdata.service_plan or userdata.service_family
        service_family, selected_plan = self._resolve_selection(userdata, selection_value)

        resolved_date = date or userdata.requested_date or "tomorrow"
        resolved_time_pref = time_preference or userdata.requested_time or "anytime"
        service_label = get_service_display_label(service_family, selected_plan)
        signature = (
            f"{service_family}|{selected_plan or ''}|{resolved_date}|{resolved_time_pref}|"
            f"{userdata.dog_size or ''}"
        ).lower()

        cached_signature = userdata.runtime_tool_facts.get("last_availability_signature")
        cached_response = userdata.runtime_tool_facts.get("last_availability_response")
        if cached_signature == signature and isinstance(cached_response, str):
            trace_log(
                logger=logger,
                flag_name="HH_TRACE_TOOLS",
                trace_id=trace_id,
                message="tool.check_availability.cached_hit",
                signature=signature,
            )
            return cached_response

        if signature in self._availability_inflight_signatures:
            trace_log(
                logger=logger,
                flag_name="HH_TRACE_TOOLS",
                trace_id=trace_id,
                message="tool.check_availability.duplicate_inflight",
                signature=signature,
                service=service,
                service_plan=service_plan,
            )
            return (
                "CHECK_IN_PROGRESS: Availability is already being checked for this same request. "
                "Do not call check_availability again; wait for the current result."
            )

        self._availability_inflight_signatures.add(signature)
        try:
            await self.session.say(
                "Sure. I'm checking availability now. Kindly wait a moment."
            )

            slots = self._get_slots(
                context=context,
                service_family=service_family,
                date=resolved_date,
                time_preference=resolved_time_pref,
            )

            userdata.service_family = service_family
            userdata.service_plan = selected_plan
            userdata.selection_source = "scheduler.check_availability"
            userdata.requested_services = [service_family]
            userdata.requested_date = resolved_date
            userdata.requested_time = resolved_time_pref
            userdata._last_checked_service = service_family
            userdata._last_checked_plan = selected_plan
            userdata._last_checked_date = resolved_date
            userdata._last_checked_time = resolved_time_pref
            userdata._last_slots = slots
            userdata.runtime_tool_facts["availability"] = {
                "service_family": service_family,
                "service_plan": selected_plan,
                "service_label": service_label,
                "date": resolved_date,
                "time_preference": resolved_time_pref,
                "times": [slot["time"] for slot in slots],
            }

            trace_log(
                logger=logger,
                flag_name="HH_TRACE_STATE",
                trace_id=trace_id,
                message="tool.check_availability.state_diff",
                changes=userdata_diff(before, userdata_snapshot(userdata)),
            )

            if not slots:
                response = (
                    f"NO_AVAILABILITY: No {service_label} slots were found for {resolved_date}. "
                    "Use suggest_alternative_times to offer nearby options."
                )
                userdata.runtime_tool_facts["last_availability_signature"] = signature
                userdata.runtime_tool_facts["last_availability_response"] = response
                return response

            available_times = ", ".join(slot["time"] for slot in slots)
            response = (
                f"AVAILABLE_TIMES for {service_label} on {resolved_date}: {available_times}\n\n"
                "IMPORTANT: Tell user only the available times and ask which one they prefer, "
                "or whether they want details for a specific time."
            )
            userdata.runtime_tool_facts["last_availability_signature"] = signature
            userdata.runtime_tool_facts["last_availability_response"] = response
            return response
        finally:
            self._availability_inflight_signatures.discard(signature)

    @function_tool
    async def get_slot_details(
        self,
        context: RunContext_T,
        time: str,
        service: str | None = None,
        service_plan: str | None = None,
        date: str | None = None,
    ) -> str:
        """Get details for one slot time, including quote data."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_TOOLS",
            trace_id=trace_id,
            message="tool.get_slot_details.call",
            time=time,
            service=service,
            service_plan=service_plan,
            date=date,
        )

        selection_value = (
            service_plan
            or service
            or getattr(userdata, "_last_checked_plan", None)
            or getattr(userdata, "_last_checked_service", None)
            or userdata.service_plan
            or userdata.service_family
        )
        service_family, selected_plan = self._resolve_selection(userdata, selection_value)
        service_label = get_service_display_label(service_family, selected_plan)

        resolved_date = date or getattr(userdata, "_last_checked_date", None) or userdata.requested_date or "tomorrow"
        resolved_time_pref = getattr(userdata, "_last_checked_time", None) or userdata.requested_time or "anytime"

        slots: list[dict] = list(getattr(userdata, "_last_slots", []))
        if not slots:
            slots = self._get_slots(
                context=context,
                service_family=service_family,
                date=resolved_date,
                time_preference=resolved_time_pref,
            )

        requested = _normalize_time_token(time)
        selected = next(
            (
                slot
                for slot in slots
                if requested in _normalize_time_token(slot["time"])
                or _normalize_time_token(slot["time"]) in requested
            ),
            None,
        )

        if not selected:
            available = ", ".join(slot["time"] for slot in slots) if slots else "none"
            return f"ERROR: No slot found for {time}. Available times: {available}"

        quote = compute_selection_quote(
            service_family=service_family,
            service_plan=selected_plan,
            dog_size=userdata.dog_size,
        )

        userdata.runtime_tool_facts["slot_details"] = {
            "service_family": service_family,
            "service_plan": selected_plan,
            "service_label": service_label,
            "date": resolved_date,
            "time": selected["time"],
            "staff": selected["staff"],
            "duration": selected["duration"],
            "quoted_total": quote["total"],
            "billing_cycle": quote["billing_cycle"],
        }

        details = (
            f"SLOT_DETAILS:\n"
            f"Service: {service_label}\n"
            f"Date: {selected['date']}\n"
            f"Time: {selected['time']}\n"
            f"Staff: {selected['staff']}\n"
            f"Duration: {selected['duration']}\n"
            f"Quote: ${float(quote['total']):.2f} ({quote['billing_cycle']})\n"
            f"Notes: {quote['quote_notes']}\n\n"
            "Tell user: Present these details and ask if they want to confirm this slot."
        )
        return details

    @function_tool
    async def book_slot(
        self,
        context: RunContext_T,
        date: str,
        time: str,
        service: str | None = None,
        service_plan: str | None = None,
    ) -> str:
        """Book a slot and persist normalized request + quote state."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        before = userdata_snapshot(userdata)
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_TOOLS",
            trace_id=trace_id,
            message="tool.book_slot.call",
            date=date,
            time=time,
            service=service,
            service_plan=service_plan,
        )

        await self.session.say(
            "Great. I'm confirming that booking now. Kindly wait a moment."
        )

        selection_value = service_plan or service or userdata.service_plan or userdata.service_family
        service_family, selected_plan = self._resolve_selection(userdata, selection_value)
        service_label = get_service_display_label(service_family, selected_plan)

        slots = self._get_slots(
            context=context,
            service_family=service_family,
            date=date,
            time_preference="anytime",
        )

        requested = _normalize_time_token(time)
        selected = next(
            (
                slot
                for slot in slots
                if requested in _normalize_time_token(slot["time"])
                or _normalize_time_token(slot["time"]) in requested
            ),
            None,
        )

        if not selected:
            available = ", ".join(slot["time"] for slot in slots) if slots else "none"
            return (
                f"BOOKING_FAILED: No {service_label} slot found for {time} on {date}. "
                f"Available times: {available}"
            )

        booking_seed = hashlib.sha256(
            f"{userdata.name}|{service_family}|{selected_plan}|{date}|{selected['time']}".encode("utf-8")
        ).hexdigest()[:8].upper()
        booking_id = f"HH-{booking_seed}"

        quote = compute_selection_quote(
            service_family=service_family,
            service_plan=selected_plan,
            dog_size=userdata.dog_size,
        )

        userdata.booking_id = booking_id
        userdata.instructor_name = selected["staff"]
        userdata.service_family = service_family
        userdata.service_plan = selected_plan
        userdata.selection_source = "scheduler.book_slot"
        userdata.requested_services = [service_family]
        userdata.requested_date = date
        userdata.requested_time = selected["time"]
        userdata.quoted_subtotal = float(quote["subtotal"])
        userdata.quoted_tax = float(quote["tax"])
        userdata.quoted_total = float(quote["total"])
        userdata.total_amount = float(quote["total"])
        userdata.quote_notes = (
            f"{service_label} with {selected['staff']} at {selected['time']} ({selected['duration']}). "
            f"{quote['quote_notes']}"
        )
        userdata.handoff_status = "pending"

        # Keep legacy fields populated for backward compatibility with existing summary paths.
        userdata.preferred_date = date
        userdata.preferred_time = selected["time"]

        userdata.runtime_tool_facts["booking"] = {
            "booking_id": booking_id,
            "service_family": service_family,
            "service_plan": selected_plan,
            "service_label": service_label,
            "date": date,
            "time": selected["time"],
            "staff": selected["staff"],
            "duration": selected["duration"],
            "quote": quote,
        }

        trace_log(
            logger=logger,
            flag_name="HH_TRACE_STATE",
            trace_id=trace_id,
            message="tool.book_slot.state_diff",
            changes=userdata_diff(before, userdata_snapshot(userdata)),
        )

        return (
            f"BOOKING_CONFIRMED:\n"
            f"Booking ID: {booking_id}\n"
            f"Service: {service_label}\n"
            f"Date: {date}\n"
            f"Time: {selected['time']}\n"
            f"Staff: {selected['staff']}\n"
            f"Quoted Total: ${float(quote['total']):.2f} ({quote['billing_cycle']})\n\n"
            "Tell user: Booking is confirmed and ask if they are ready to finalize the request."
        )

    @function_tool
    async def suggest_alternative_times(
        self,
        context: RunContext_T,
        service: str | None = None,
        service_plan: str | None = None,
        date: str | None = None,
        reason: str = "Requested time unavailable",
    ) -> str:
        """Suggest concise morning/afternoon alternatives."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)

        selection_value = service_plan or service or userdata.service_plan or userdata.service_family
        service_family, selected_plan = self._resolve_selection(userdata, selection_value)
        service_label = get_service_display_label(service_family, selected_plan)
        resolved_date = date or userdata.requested_date or "tomorrow"

        morning_slots = self._get_slots(
            context=context,
            service_family=service_family,
            date=resolved_date,
            time_preference="morning",
        )
        afternoon_slots = self._get_slots(
            context=context,
            service_family=service_family,
            date=resolved_date,
            time_preference="afternoon",
        )

        morning_times = [slot["time"] for slot in morning_slots[:3]]
        afternoon_times = [slot["time"] for slot in afternoon_slots[:3]]
        userdata.runtime_tool_facts["alternatives"] = {
            "service_family": service_family,
            "service_plan": selected_plan,
            "service_label": service_label,
            "date": resolved_date,
            "morning_times": morning_times,
            "afternoon_times": afternoon_times,
            "reason": reason,
        }

        trace_log(
            logger=logger,
            flag_name="HH_TRACE_TOOLS",
            trace_id=trace_id,
            message="tool.suggest_alternative_times",
            service_family=service_family,
            service_plan=selected_plan,
            date=resolved_date,
            reason=reason,
            morning_times=morning_times,
            afternoon_times=afternoon_times,
        )

        return (
            f"ALTERNATIVE_TIMES for {service_label} (Reason: {reason}):\n"
            f"MORNING: {', '.join(morning_times) if morning_times else 'none'}\n"
            f"AFTERNOON: {', '.join(afternoon_times) if afternoon_times else 'none'}\n\n"
            "Tell user: Share these times briefly and ask which option they prefer."
        )

    @function_tool
    async def transfer_to_billing(self, context: RunContext_T) -> BaseAgent:
        """Transfer booking to BillingAgent for final confirmation + handoff."""
        from agents.billing_agent import BillingAgent

        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        if not userdata.is_booking_complete():
            return "BLOCKED: Confirm service, date, and time before moving to billing."
        if not userdata.booking_id:
            return "BLOCKED: Confirm a specific booking slot before moving to billing."
        if userdata.quoted_total is None:
            return "BLOCKED: Quoted total is missing. Confirm slot details first."

        userdata.handoff_pending_action = "billing_on_enter"
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="scheduler.transfer_to_billing",
            from_agent="SchedulerAgent",
            to_agent="BillingAgent",
            summary=userdata.summarize(),
            runtime_tool_facts=userdata.runtime_tool_facts,
        )

        await self.session.say(
            "Great, your service request and quote are ready. "
            "I'm now transferring your call to our Billing Department to confirm and finalize. "
            "Kindly wait a moment while I connect you."
        )

        # Gear handoff is intentionally bypassed in this flow.
        # To re-enable later, route Scheduler -> Gear -> Billing.
        return BillingAgent(chat_ctx=self.chat_ctx)
