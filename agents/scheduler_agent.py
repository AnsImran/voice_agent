"""Scheduler agent for Happy Hound service availability and booking."""
from __future__ import annotations

import asyncio
import functools
import logging
import re

from livekit.agents.llm import function_tool

from .base_agent import BaseAgent, RunContext_T
from tools.availability_provider import (
    AvailabilityProvider,
    MockAvailabilityProvider,
    compute_selection_quote,
    get_service_display_label,
    resolve_service_selection,
)
from tools.gingr_availability import determine_service_availability, service_name_looks_grooming
from tools.handoff_email_tools import build_handoff_payload, send_handoff_email
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


_VAGUE_TIME_WORDS = {"morning", "afternoon", "evening", "anytime", "any", "any time", "flexible", ""}


def _format_slot_datetime(iso_str: str) -> str:
    """Convert a Gingr ISO datetime to a voice-friendly string.

    '2026-03-25T11:30' -> '11:30 AM on Tuesday March 25'
    '2026-03-25T09:00' -> '9:00 AM on Tuesday March 25'
    """
    from datetime import datetime as _dt
    dt = _dt.fromisoformat(iso_str)
    hour = dt.hour % 12 or 12
    minute = dt.minute
    ampm = "AM" if dt.hour < 12 else "PM"
    time_part = f"{hour}:{minute:02d} {ampm}"
    date_part = dt.strftime("%A %B ") + str(dt.day)
    return f"{time_part} on {date_part}"


def _parse_time_to_hhmm(value: str) -> str | None:
    """Convert a caller time expression to HH:MM, or None if vague/unrecognised.

    Examples: "9am" → "09:00", "2:30pm" → "14:30", "14:00" → "14:00",
              "morning" → None, "anytime" → None.
    """
    val = (value or "").strip().lower()
    if val in _VAGUE_TIME_WORDS:
        return None
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(?:\s*(am|pm))?$", val.replace(" ", ""))
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    suffix = m.group(3)
    if suffix == "pm" and hour != 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


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

        await self.session.say(
            "Welcome to the Scheduling Department at Happy Hound. "
            "Let me check availability for you."
        )

        if missing:
            await self.session.generate_reply(
                instructions=(
                    "You are now the SchedulerAgent. You have already greeted the caller "
                    "with the department welcome — do not repeat it. Continue immediately "
                    "and ask only for the missing details: "
                    f"{', '.join(missing)}. "
                    "If the caller previously selected Golden Leash Club Card, preserve that plan."
                )
            )
            return

        await self.session.generate_reply(
            instructions=(
                "You have already greeted the caller with the department welcome — "
                "do not repeat it. Briefly confirm the known service and schedule "
                "preferences and immediately call check_availability."
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

    def _quote_basis(self, userdata) -> dict:
        return {
            "service_family": userdata.service_family
            or (userdata.requested_services[0] if userdata.requested_services else "daycare"),
            "service_plan": userdata.service_plan,
            "dog_size": userdata.dog_size,
        }

    def _ensure_quote(self, userdata, force_recompute: bool = False) -> dict:
        basis = self._quote_basis(userdata)
        stored_basis = userdata.runtime_tool_facts.get("quote_basis")

        should_recompute = force_recompute or (
            userdata.quoted_subtotal is None
            or userdata.quoted_tax is None
            or userdata.quoted_total is None
            or stored_basis != basis
        )

        if should_recompute:
            quote = compute_selection_quote(
                service_family=basis["service_family"],
                service_plan=basis["service_plan"],
                dog_size=basis["dog_size"],
            )
            userdata.quoted_subtotal = float(quote["subtotal"])
            userdata.quoted_tax = float(quote["tax"])
            userdata.quoted_total = float(quote["total"])
            userdata.quote_notes = str(quote["quote_notes"])
            userdata.total_amount = float(quote["total"])
            userdata.runtime_tool_facts["quote_basis"] = basis
            userdata.runtime_tool_facts["active_quote"] = quote

        if not userdata.handoff_status:
            userdata.handoff_status = "pending"

        service_label = get_service_display_label(
            basis["service_family"],
            basis["service_plan"],
        )
        return {
            "service_label": service_label,
            "billing_cycle": userdata.runtime_tool_facts.get("active_quote", {}).get(
                "billing_cycle", "per_visit"
            ),
        }

    @function_tool
    async def check_availability(
        self,
        context: RunContext_T,
        service: str | None = None,
        service_plan: str | None = None,
        date: str | None = None,
        time_preference: str | None = None,
        ) -> str:
        """Check service availability. For grooming, validates a specific requested time via the Gingr API."""
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

            # --- Grooming path: real Gingr check ---
            # Triggered when:
            #   (a) the resolved service family is grooming, OR
            #   (b) the raw service string is a grooming add-on on a boarding/daycare/training
            #       visit (e.g. "Boarding + Deluxe Bath", "Daycare + A la Carte").
            is_grooming = service_family == "grooming" or service_name_looks_grooming(service)
            if is_grooming:
                hhmm = _parse_time_to_hhmm(resolved_time_pref)
                if hhmm is None:
                    response = (
                        "GROOMING_NEEDS_SPECIFIC_TIME: Grooming appointments are booked at a specific "
                        "time. Ask the caller: 'What time would you like for your grooming appointment? "
                        "For example, 9am or 2pm.' Do not call check_availability again until you have "
                        "a specific time from the caller."
                    )
                    userdata.runtime_tool_facts["last_availability_signature"] = signature
                    userdata.runtime_tool_facts["last_availability_response"] = response
                    return response

                # Determine the specific grooming service name (e.g. "Deluxe Bath", "A la Carte").
                # Exclude generic top-level category labels that carry no duration signal.
                svc_lower = (service or "").strip().lower()
                specific_service: str | None = (
                    service
                    if service and svc_lower not in {"grooming", "groom", "boarding", "daycare", "training"}
                    else None
                )

                # Diagnostic: confirm env vars are visible before the API call.
                _gingr_key_len = len(
                    __import__("os").environ.get("GINGR_API_KEY", "").strip().strip('"').strip("'")
                )
                print(
                    f"\n[GINGR] check_availability pre-call:"
                    f"\n  category       : {service_family}"
                    f"\n  service        : {specific_service or '(generic)'}"
                    f"\n  date           : {resolved_date}"
                    f"\n  hhmm           : {hhmm}"
                    f"\n  api_key_len    : {_gingr_key_len} (0 = secret not loaded)\n",
                    flush=True,
                )

                try:
                    # Run the blocking Gingr HTTP calls in a thread so the
                    # asyncio event loop stays responsive during the network wait.
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(
                        None,
                        functools.partial(
                            determine_service_availability,
                            category=service_family.capitalize(),
                            requested_date=resolved_date,
                            requested_start_hhmm=hhmm,
                            requested_service=specific_service,
                            explicit_duration=60 if specific_service is None else None,
                        ),
                    )
                except Exception as exc:
                    logger.warning("gingr.check_availability failed: %r", exc, exc_info=True)
                    response = (
                        f"GROOMING_CHECK_ERROR: Could not reach the scheduling system ({exc}). "
                        "Tell the caller you are unable to check grooming availability right now and "
                        "offer to have a team member call back."
                    )
                    userdata.runtime_tool_facts["last_availability_signature"] = signature
                    userdata.runtime_tool_facts["last_availability_response"] = response
                    return response

                # Always print Gingr result to console for independent verification.
                print(
                    f"\n[GINGR] check_availability result:"
                    f"\n  category       : {service_family}"
                    f"\n  service        : {specific_service or '(generic grooming)'}"
                    f"\n  date           : {resolved_date}"
                    f"\n  requested_start: {result.requested_start}"
                    f"\n  requested_end  : {result.requested_end}"
                    f"\n  duration_min   : {result.duration_minutes}"
                    f"\n  available      : {result.available}"
                    f"\n  reason         : {result.reason}"
                    f"\n  next_available : {result.next_available_start}"
                    f"\n  occupied_slots : {len(result.occupied_slots)} slot(s)"
                    f"\n  segment_checks : {len(result.segment_checks)} segment(s)\n",
                    flush=True,
                )

                userdata.service_family = service_family
                userdata.service_plan = selected_plan
                userdata.selection_source = "scheduler.check_availability"
                userdata.requested_services = [service_family]
                userdata.requested_date = resolved_date
                userdata.requested_time = hhmm
                userdata._last_checked_service = service_family
                userdata._last_checked_plan = selected_plan
                userdata._last_checked_date = resolved_date
                userdata._last_checked_time = hhmm
                userdata.runtime_tool_facts["last_gingr_result"] = {
                    "available": result.available,
                    "reason": result.reason,
                    "requested_start": result.requested_start,
                    "requested_end": result.requested_end,
                    "duration_minutes": result.duration_minutes,
                    "next_available_start": result.next_available_start,
                    "requested_service": result.requested_service,
                    "category": service_family,
                }
                userdata.runtime_tool_facts["availability"] = {
                    "service_family": service_family,
                    "service_plan": selected_plan,
                    "service_label": service_label,
                    "date": resolved_date,
                    "time_preference": hhmm,
                    "times": [hhmm] if result.available else [],
                }

                trace_log(
                    logger=logger,
                    flag_name="HH_TRACE_STATE",
                    trace_id=trace_id,
                    message="tool.check_availability.gingr_result",
                    available=result.available,
                    reason=result.reason,
                    next_available=result.next_available_start,
                    occupied_slots=len(result.occupied_slots),
                    changes=userdata_diff(before, userdata_snapshot(userdata)),
                )

                # Build caller-facing label: for mixed cases, name the add-on specifically.
                grooming_label = (
                    specific_service if specific_service and service_family != "grooming"
                    else service_label
                )

                if result.available:
                    response = (
                        f"GROOMING_AVAILABLE: {grooming_label} on {resolved_date} at {hhmm} is open.\n"
                        "Groomer will be assigned by the Happy Hound team at the time of appointment.\n\n"
                        "Tell user: That time is available. Ask if they would like to confirm this slot."
                    )
                else:
                    if result.reason == "outside_staffing_hours":
                        reason_msg = "that time is outside grooming hours"
                    else:
                        reason_msg = "the grooming schedule is full at that time"
                    if result.next_available_start:
                        next_human = _format_slot_datetime(result.next_available_start)
                        response = (
                            f"GROOMING_UNAVAILABLE: {hhmm} on {resolved_date} is not available "
                            f"({reason_msg}). Next available grooming slot: {next_human}.\n\n"
                            f"Tell user: That time is not available. Offer {next_human} as "
                            "the next open slot and ask if they would like to book that instead."
                        )
                    else:
                        response = (
                            f"GROOMING_UNAVAILABLE: {hhmm} on {resolved_date} is not available "
                            f"({reason_msg}). No nearby grooming slots found within 7 days. "
                            "Tell user: That time is not available and suggest trying a different date."
                        )

                userdata.runtime_tool_facts["last_availability_signature"] = signature
                userdata.runtime_tool_facts["last_availability_response"] = response
                return response

            # --- Non-grooming path: use mock provider (daycare / boarding / training) ---
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

        quote = compute_selection_quote(
            service_family=service_family,
            service_plan=selected_plan,
            dog_size=userdata.dog_size,
        )

        # --- Grooming path: use Gingr result, no mock slot lookup ---
        if service_family == "grooming" or service_name_looks_grooming(service):
            gingr = userdata.runtime_tool_facts.get("last_gingr_result", {})
            if not gingr.get("available"):
                return (
                    "ERROR: No confirmed grooming slot found. "
                    "Please call check_availability with a specific time first."
                )
            confirmed_time = gingr.get("requested_start") or time
            duration_min = gingr.get("duration_minutes") or 60
            duration_str = f"{duration_min} minutes"
            specific_service = gingr.get("requested_service") or service_label

            userdata.runtime_tool_facts["slot_details"] = {
                "service_family": service_family,
                "service_plan": selected_plan,
                "service_label": service_label,
                "date": resolved_date,
                "time": confirmed_time,
                "staff": "Happy Hound Grooming Team",
                "duration": duration_str,
                "quoted_total": quote["total"],
                "billing_cycle": quote["billing_cycle"],
            }
            return (
                f"SLOT_DETAILS:\n"
                f"Service: {specific_service}\n"
                f"Date: {resolved_date}\n"
                f"Time: {confirmed_time}\n"
                f"Staff: Happy Hound Grooming Team (assigned at appointment)\n"
                f"Duration: {duration_str}\n"
                f"Quote: ${float(quote['total']):.2f} ({quote['billing_cycle']})\n"
                f"Notes: {quote['quote_notes']}\n\n"
                "Tell user: Present these details and ask if they want to confirm this slot."
            )

        # --- Non-grooming path: look up from cached mock slots ---
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

        return (
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

    @function_tool
    async def book_slot(
        self,
        context: RunContext_T,
        date: str,
        time: str,
        service: str | None = None,
        service_plan: str | None = None,
    ) -> "BaseAgent | str":
        """Confirm a slot and transfer to Intake to collect details and finalize the booking."""
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
            "Great. I'm confirming that slot now. Kindly wait a moment."
        )

        selection_value = service_plan or service or userdata.service_plan or userdata.service_family
        service_family, selected_plan = self._resolve_selection(userdata, selection_value)
        service_label = get_service_display_label(service_family, selected_plan)

        # --- Grooming path: confirm from Gingr result, no mock slot lookup ---
        if service_family == "grooming" or service_name_looks_grooming(service):
            gingr = userdata.runtime_tool_facts.get("last_gingr_result", {})

            # If the last Gingr result is unavailable, check whether the LLM is
            # trying to book the "next available" slot it already surfaced to the
            # caller.  In that case we re-run the Gingr check inline rather than
            # returning BOOKING_FAILED and forcing an extra LLM round-trip of
            # check_availability → book_slot.
            if not gingr.get("available"):
                req_hhmm = _parse_time_to_hhmm(time)
                next_avail_iso = gingr.get("next_available_start", "")
                # "2026-03-26T13:30" → "13:30"
                next_hhmm = next_avail_iso[11:16] if len(next_avail_iso) >= 16 else ""
                if req_hhmm and next_hhmm and req_hhmm == next_hhmm:
                    # The requested time matches the suggested next-available slot.
                    # Re-verify it directly so we avoid the LLM round-trip.
                    specific_service: str | None = (
                        service
                        if service and service.strip().lower() not in {
                            "grooming", "groom", "boarding", "daycare", "training"
                        }
                        else None
                    )
                    loop = asyncio.get_event_loop()
                    fresh = await loop.run_in_executor(
                        None,
                        functools.partial(
                            determine_service_availability,
                            category=service_family.capitalize(),
                            requested_date=date,
                            requested_start_hhmm=req_hhmm,
                            requested_service=specific_service,
                            explicit_duration=60 if specific_service is None else None,
                        ),
                    )
                    print(
                        f"\n[GINGR] book_slot inline re-check:\n"
                        f"  category       : {service_family}\n"
                        f"  service        : {specific_service or service}\n"
                        f"  date           : {date}\n"
                        f"  requested_start: {fresh.requested_start}\n"
                        f"  available      : {fresh.available}\n"
                        f"  reason         : {fresh.reason}\n",
                        flush=True,
                    )
                    gingr = {
                        "available": fresh.available,
                        "reason": fresh.reason,
                        "requested_start": fresh.requested_start,
                        "requested_end": fresh.requested_end,
                        "duration_minutes": fresh.duration_minutes,
                        "next_available_start": fresh.next_available_start,
                        "requested_service": fresh.requested_service,
                        "category": fresh.category,
                    }
                    userdata.runtime_tool_facts["last_gingr_result"] = gingr
                    if not gingr["available"]:
                        return (
                            f"BOOKING_FAILED: The {service_label} slot at {time} on {date} is no longer "
                            f"available (reason: {fresh.reason}). "
                            "Call check_availability again to find the next open slot."
                        )
                else:
                    return (
                        "BOOKING_FAILED: No confirmed grooming slot found. "
                        "Please call check_availability with a specific time first."
                    )

            confirmed_time = gingr.get("requested_start") or time
            duration_min = gingr.get("duration_minutes") or 60
            duration_str = f"{duration_min} minutes"
            staff = "Happy Hound Grooming Team"

            userdata.service_family = service_family
            userdata.service_plan = selected_plan
            userdata.selection_source = "scheduler.book_slot"
            userdata.requested_services = [service_family]
            userdata.requested_date = date
            userdata.requested_time = confirmed_time

            userdata.runtime_tool_facts["confirmed_slot"] = {
                "service_family": service_family,
                "service_plan": selected_plan,
                "service_label": service_label,
                "date": date,
                "time": confirmed_time,
                "staff": staff,
                "duration": duration_str,
            }

            trace_log(
                logger=logger,
                flag_name="HH_TRACE_STATE",
                trace_id=trace_id,
                message="tool.book_slot.slot_confirmed_grooming",
                changes=userdata_diff(before, userdata_snapshot(userdata)),
            )

            handle = await self.session.say(
                "Slot confirmed! I'm now transferring you to our Intake Department "
                "to finalize your booking. Kindly wait a moment."
            )
            try:
                await handle.wait_for_playout()
            except Exception:
                pass
            from agents.intake_agent import IntakeAgent
            return IntakeAgent(chat_ctx=self.chat_ctx)

        # --- Non-grooming path: find slot from mock provider ---
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

        userdata.service_family = service_family
        userdata.service_plan = selected_plan
        userdata.selection_source = "scheduler.book_slot"
        userdata.requested_services = [service_family]
        userdata.requested_date = date
        userdata.requested_time = selected["time"]

        userdata.runtime_tool_facts["confirmed_slot"] = {
            "service_family": service_family,
            "service_plan": selected_plan,
            "service_label": service_label,
            "date": date,
            "time": selected["time"],
            "staff": selected["staff"],
            "duration": selected["duration"],
        }

        trace_log(
            logger=logger,
            flag_name="HH_TRACE_STATE",
            trace_id=trace_id,
            message="tool.book_slot.slot_confirmed_non_grooming",
            changes=userdata_diff(before, userdata_snapshot(userdata)),
        )

        handle = await self.session.say(
            "Slot confirmed! I'm now transferring you to our Intake Department "
            "to finalize your booking. Kindly wait a moment."
        )
        try:
            await handle.wait_for_playout()
        except Exception:
            pass
        from agents.intake_agent import IntakeAgent
        return IntakeAgent(chat_ctx=self.chat_ctx)

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
    async def calculate_total(self, context: RunContext_T) -> str:
        """Show quote breakdown for the current request."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        before = userdata_snapshot(userdata)

        trace_log(
            logger=logger,
            flag_name="HH_TRACE_TOOLS",
            trace_id=trace_id,
            message="tool.calculate_total.call",
        )
        quote_meta = self._ensure_quote(userdata, force_recompute=True)

        trace_log(
            logger=logger,
            flag_name="HH_TRACE_STATE",
            trace_id=trace_id,
            message="tool.calculate_total.state_diff",
            changes=userdata_diff(before, userdata_snapshot(userdata)),
        )

        return (
            "COST_BREAKDOWN:\n"
            f"Service: {quote_meta['service_label']}\n"
            f"Billing Cycle: {quote_meta['billing_cycle']}\n"
            f"Subtotal: ${userdata.quoted_subtotal:.2f}\n"
            f"Tax: ${userdata.quoted_tax:.2f}\n"
            f"TOTAL: ${userdata.quoted_total:.2f}\n"
            f"Notes: {userdata.quote_notes}\n"
            "Tell user: Read the total and ask if they want you to send this request now."
        )

    @function_tool
    async def send_structured_handoff(
        self,
        context: RunContext_T,
        notes: str = "",
    ) -> str:
        """Send structured session state to staff via SMTP handoff email."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        before = userdata_snapshot(userdata)
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_TOOLS",
            trace_id=trace_id,
            message="tool.send_structured_handoff.call",
            notes=notes,
        )

        await self.session.say(
            "Understood. I'm sending your request details to our team now. "
            "Kindly wait a moment."
        )

        self._ensure_quote(userdata, force_recompute=False)

        if notes.strip():
            extra_notes = notes.strip()
            userdata.quote_notes = (
                f"{userdata.quote_notes} | Additional caller note: {extra_notes}"
                if userdata.quote_notes
                else f"Additional caller note: {extra_notes}"
            )

        payload = build_handoff_payload(userdata)
        userdata.handoff_status = "pending"

        try:
            result = send_handoff_email(payload)
        except ValueError as exc:
            userdata.handoff_status = "failed"
            trace_log(
                logger=logger,
                flag_name="HH_TRACE_STATE",
                trace_id=trace_id,
                message="tool.send_structured_handoff.config_error",
                error=str(exc),
                changes=userdata_diff(before, userdata_snapshot(userdata)),
            )
            return (
                f"HANDOFF_CONFIG_ERROR: {exc}. "
                "Tell user: I have your request, but there is a system configuration issue. "
                "A team member will follow up shortly."
            )
        except RuntimeError as exc:
            userdata.handoff_status = "failed"
            trace_log(
                logger=logger,
                flag_name="HH_TRACE_STATE",
                trace_id=trace_id,
                message="tool.send_structured_handoff.send_error",
                error=str(exc),
                changes=userdata_diff(before, userdata_snapshot(userdata)),
            )
            return (
                f"HANDOFF_SEND_FAILED: {exc}. "
                "Tell user: I could not send the handoff right now, but your request is saved and "
                "our team will still follow up."
            )

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
            message="tool.send_structured_handoff.state_diff",
            changes=userdata_diff(before, userdata_snapshot(userdata)),
        )

        return (
            f"HANDOFF_SENT: Message ID {result['message_id']}. "
            f"Subject: {result['subject']}. "
            "Tell user: Everything is submitted and staff will contact them shortly."
        )

    @function_tool
    async def mark_handoff_pending(
        self,
        context: RunContext_T,
        reason: str = "Customer is still deciding",
    ) -> str:
        """Mark session as pending when caller is not ready to finalize yet."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        userdata.handoff_status = "pending"
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_TOOLS",
            trace_id=trace_id,
            message="tool.mark_handoff_pending",
            reason=reason,
        )
        return (
            f"HANDOFF_PENDING: {reason}. "
            "Tell user: No problem, you can continue whenever ready."
        )

    @function_tool
    async def return_to_frontdesk(self, context: RunContext_T) -> BaseAgent:
        """Return customer to front desk for additional help."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="scheduler.return_to_frontdesk",
            from_agent="SchedulerAgent",
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
    async def transfer_to_billing(self, context: RunContext_T) -> str:
        """Legacy alias kept for compatibility; billing finalization now runs in Scheduler."""

        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        if not userdata.is_booking_complete():
            return "BLOCKED: Confirm service, date, and time before finalizing."
        if not userdata.booking_id:
            return "BLOCKED: Confirm a specific booking slot before finalizing."

        quote_meta = self._ensure_quote(userdata, force_recompute=True)
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="scheduler.transfer_to_billing_legacy_alias",
            from_agent="SchedulerAgent",
            summary=userdata.summarize(),
        )
        return (
            "BILLING_MERGED: Final confirmation and handoff now happen in Scheduler. "
            f"Current total is ${userdata.quoted_total:.2f} ({quote_meta['billing_cycle']}). "
            "Tell user: confirm the total, and if approved call send_structured_handoff."
        )
