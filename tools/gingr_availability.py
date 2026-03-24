"""
happy_hound_grooming_availability.py
====================================

Purpose
-------
This module implements the final availability strategy that came out of the
Happy Hound / Gingr investigation.

Final business decision
-----------------------
1. Daycare and boarding do NOT use hard capacity math in this agent.
   The agent should simply report availability for those categories.
2. Grooming DOES use real availability logic.
3. Any reservation category that INCLUDES a grooming-related service
   (for example: a boarding reservation with a bath add-on, or a daycare
   reservation with an a-la-carte grooming service) must also use the
   grooming availability logic, because those services still consume groomer
   time.
4. The correct Gingr endpoint for this logic is:
       POST /api/v1/reservations
   because the raw reservation payload contains the service-level schedule
   data we need.
5. The correct data to analyze is NOT the parent reservation date range.
   Instead, the correct data is each service row's:
       - scheduled_at
       - scheduled_until
       - assigned_to
       - name

Why this file exists
--------------------
Earlier exploratory scripts helped discover payload shape, but they were not
well suited to direct integration into a production agent. This file is the
clean, documented, integration-ready version.

Design goals
------------
- Be easy for a larger agent to call as a library.
- Be easy to run from the command line while testing.
- Be explicit and heavily documented.
- Keep business rules separate from raw Gingr parsing.
- Support future staffing changes such as "1 groomer in the morning,
  2 groomers in the afternoon".
- Support both live Gingr API calls and local JSON payload replay for tests.
"""

# Standard library imports used for argument parsing, JSON handling,
# date/time arithmetic, regular expressions, and structured result objects.
import argparse
import json
import os
import re
import sys

# Dataclasses make result payloads explicit and easy to serialize.
from dataclasses import asdict, dataclass

# Datetime utilities are used to compare requested slots with existing
# service intervals returned by Gingr.
from datetime import date, datetime, time, timedelta

# Type hints improve readability and make this module easier to integrate.
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Requests is used for live Gingr API calls.
import requests


# ---------------------------------------------------------------------------
# Environment-backed configuration
# ---------------------------------------------------------------------------
# These values are read from environment variables so the code can be safely
# reused across environments and tenants without hardcoding secrets.
#
# IMPORTANT:
# - GINGR_API_KEY must be supplied in the environment for live API mode.
# - GINGR_API_BASE should point to the fully qualified Gingr API base.
# - GINGR_LOCATION_ID defaults to 1 because the investigated tenant data
#   showed Happy Hound Oakland as location 1.
GINGR_API_KEY = os.environ.get("GINGR_API_KEY", "")
GINGR_TENANT = os.environ.get("GINGR_TENANT", "happyhound")
GINGR_API_BASE = os.environ.get(
    "GINGR_API_BASE",
    f"https://{GINGR_TENANT}.gingrapp.com/api/v1",
)
GINGR_LOCATION_ID = os.environ.get("GINGR_LOCATION_ID", "1")


# ---------------------------------------------------------------------------
# Default staffing rules
# ---------------------------------------------------------------------------
# Weekday numbering follows Python's convention:
#   Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
#
# These defaults encode the business rule gathered during investigation:
# - Tuesday: 7am-1pm
# - Wednesday-Saturday: 7am-5pm
# - Sunday/Monday: closed for grooming
#
# The integer in each tuple is the number of groomers available in that window.
# The structure is:
#   weekday -> list[(start_hhmm, end_hhmm, worker_count)]
DEFAULT_STAFFING_RULES: Dict[int, List[Tuple[str, str, int]]] = {
    0: [],
    1: [("07:00", "13:00", 1)],
    2: [("07:00", "17:00", 1)],
    3: [("07:00", "17:00", 1)],
    4: [("07:00", "17:00", 1)],
    5: [("07:00", "17:00", 1)],
    6: [],
}


# ---------------------------------------------------------------------------
# Default duration map for NEW requested bookings
# ---------------------------------------------------------------------------
# Existing bookings should always trust Gingr's scheduled_at / scheduled_until
# intervals. Those are the true scheduled durations.
#
# This table is only used when checking a NEW requested booking.
# If a service is ambiguous or varies by dog size, the caller can pass an
# explicit duration instead.
DEFAULT_SERVICE_DURATION_MINUTES: Dict[str, int] = {
    "A LA CARTE": 15,
    "BASIC BATH": 60,
    "BASIC BATH PLUS": 60,
    "DELUXE BATH": 60,
    "DELUXE BATH PLUS": 90,
    "FULL GROOM": 120,
    "MINI GROOM": 120,
    "SHED LESS BATH": 120,
    "SHED-LESS BATH": 120,
    "LAST DAY BATH": 60,
    "DE SKUNK TREATMENT": 60,
    "DE-SKUNK TREATMENT": 60,
}


# ---------------------------------------------------------------------------
# Search and parsing constants
# ---------------------------------------------------------------------------
# The next available slot search advances in 15-minute increments because the
# live payloads clearly used 15-minute service boundaries.
DEFAULT_STEP_MINUTES = 15

# By default, when a requested slot is unavailable, the code looks ahead up to
# seven days to find the next available start time.
DEFAULT_LOOKAHEAD_DAYS = 7

# This regex is used to detect whether a service is likely grooming-related.
# It intentionally includes both direct grooming words and bath-related words.
GROOMING_NAME_RE = re.compile(
    r"(groom|bath|shed.?less|a\s*la\s*carte|nail|deshed|deskunk|de-skunk|skunk)",
    re.IGNORECASE,
)

# This regex is used to detect whether a service is assigned to a groomer.
# In the investigated tenant, assigned_to="Groomer" was one of the most useful
# signals of real groomer occupancy.
GROOMER_ASSIGNEE_RE = re.compile(r"groom", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Dataclasses used for clean structured results
# ---------------------------------------------------------------------------
@dataclass
class OccupiedSlot:
    """
    A normalized same-day grooming interval that actually consumes groomer time.

    Each OccupiedSlot comes from one Gingr service row inside one reservation.
    This object intentionally stores both timing and business context so the
    caller can debug why a requested slot was accepted or rejected.
    """

    reservation_id: str
    reservation_type_id: str
    reservation_type_name: str
    animal_name: str
    service_id: str
    service_name: str
    assigned_to: Optional[str]
    start: str
    end: str


@dataclass
class SegmentCheck:
    """
    A small time segment inside a requested interval.

    The checker splits the requested interval into smaller segments at every
    relevant boundary so it can ask, for each segment:
    - how many groomers are working?
    - how many existing grooming bookings overlap this segment?
    - is there still capacity left?
    """

    start: str
    end: str
    workers: int
    overlapping_bookings: int
    ok: bool


@dataclass
class AvailabilityResult:
    """
    Final structured result returned to the caller.

    This object is designed to be:
    - easy to serialize to JSON
    - easy to inspect in logs
    - easy for an upstream agent to convert into natural language
    """

    category: str
    requested_service: Optional[str]
    date: str
    requested_start: Optional[str]
    requested_end: Optional[str]
    duration_minutes: Optional[int]
    available: bool
    reason: str
    next_available_start: Optional[str]
    location_id: str
    occupied_slots: List[Dict[str, Any]]
    segment_checks: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Basic utility helpers
# ---------------------------------------------------------------------------
def fail(message: str, exit_code: int = 1) -> None:
    """
    Print a message to stderr and exit.

    Parameters
    ----------
    message:
        Human-readable error message.
    exit_code:
        Process exit code to use.
    """

    # Print the error to stderr so command-line callers can distinguish it
    # from normal JSON output.
    print(message, file=sys.stderr)

    # Exit the process immediately.
    raise SystemExit(exit_code)



def parse_date(value: str) -> date:
    """
    Parse a YYYY-MM-DD string into a date object.
    """

    # Delegate to Python's ISO parser so invalid dates raise clean errors.
    return date.fromisoformat(value)



def parse_hhmm(value: str) -> time:
    """
    Parse a HH:MM string into a time object.
    """

    # Delegate to Python's ISO parser for robust validation.
    return time.fromisoformat(value)



def combine_local(day_value: date, hhmm: str) -> datetime:
    """
    Combine a date and HH:MM string into a naive local datetime.

    We treat all comparisons in the business's local wall-clock time.
    Gingr timestamps already include an offset, but once parsed we normalize
    them to naive local datetimes so every comparison happens in one consistent
    local time frame.
    """

    # Build a naive datetime using the business-local day and time.
    return datetime.combine(day_value, parse_hhmm(hhmm))



def parse_local_iso(iso_text: str) -> datetime:
    """
    Parse a Gingr ISO timestamp and drop timezone info after parsing.

    Example input:
        2026-02-24T08:15:00-08:00

    Why we drop the timezone after parsing:
    - the payload already represents local business time
    - all comparisons in this module are performed in that same local clock
    - removing tzinfo keeps later logic simple and consistent
    """

    # Convert the ISO text into a datetime and then drop tzinfo so the result
    # becomes a naive local wall-clock datetime.
    return datetime.fromisoformat(iso_text).replace(tzinfo=None)



def normalize_service_name(service_name: str) -> str:
    """
    Normalize a service name for robust duration-map lookup.

    This removes punctuation differences and normalizes spacing so names like
    "Shed-less Bath" and "Shed less bath" can be treated the same.
    """

    # Replace non-alphanumeric runs with spaces.
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", service_name or "").strip().upper()

    # Collapse multiple spaces into a single space.
    cleaned = re.sub(r"\s+", " ", cleaned)

    # Return the normalized value.
    return cleaned



def overlaps(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime,
    b_end: datetime,
) -> bool:
    """
    Return True if two half-open intervals overlap.

    The intervals are interpreted as [start, end), which means:
    - touching endpoints do NOT count as overlap
    - 08:00-09:00 and 09:00-10:00 are adjacent, not overlapping
    """

    # Two half-open intervals overlap when each starts before the other ends.
    return a_start < b_end and b_start < a_end



def round_up_to_step(moment: datetime, step_minutes: int) -> datetime:
    """
    Round a datetime up to the next step boundary.

    This is used when searching for the next available slot in fixed increments.
    """

    # Normalize away seconds and microseconds first.
    moment = moment.replace(second=0, microsecond=0)

    # If the increment is 1 minute or less, the normalized moment is already
    # the best value we can return.
    if step_minutes <= 1:
        return moment

    # Compute how far the current minute is from the next step boundary.
    remainder = moment.minute % step_minutes

    # If we are already on a boundary, return as-is.
    if remainder == 0:
        return moment

    # Otherwise add just enough minutes to reach the next boundary.
    return moment + timedelta(minutes=(step_minutes - remainder))


# ---------------------------------------------------------------------------
# Business-rule classification helpers
# ---------------------------------------------------------------------------
def service_name_looks_grooming(service_name: Optional[str]) -> bool:
    """
    Return True when a service name suggests groomer time is involved.

    This should be used for incoming user requests as well as Gingr service rows.
    """

    # Convert None to an empty string for regex testing.
    text = service_name or ""

    # Return whether the service name matches common grooming keywords.
    return bool(GROOMING_NAME_RE.search(text))



def assignee_looks_groomer(assigned_to: Optional[str]) -> bool:
    """
    Return True when a service assignee looks like a groomer role.
    """

    # Convert None to an empty string for regex testing.
    text = assigned_to or ""

    # Return whether the assignee label suggests the Groomer role.
    return bool(GROOMER_ASSIGNEE_RE.search(text))



def request_requires_grooming_logic(category: str, requested_service: Optional[str]) -> bool:
    """
    Decide whether the request must go through grooming availability logic.

    Rules
    -----
    - If the category itself is grooming, return True.
    - If the requested service name looks grooming-related, return True.
      This covers cases like:
          * boarding + bath
          * daycare + mini groom
          * training + last day bath
    - Otherwise return False.
    """

    # Normalize the category once for stable comparisons.
    category_text = (category or "").strip().lower()

    # Direct grooming categories must use grooming logic.
    if "groom" in category_text:
        return True

    # Requests with grooming-like service names must also use grooming logic,
    # even when the top-level category is daycare, boarding, or training.
    if service_name_looks_grooming(requested_service):
        return True

    # Everything else can use the baked-in non-grooming rule.
    return False



def resolve_duration_minutes(
    requested_service: Optional[str],
    explicit_duration: Optional[int],
) -> int:
    """
    Resolve the duration for a NEW requested booking.

    Priority order
    --------------
    1. Explicit duration supplied by the caller.
    2. Duration map lookup using normalized service name.
    3. Fuzzy containment lookup in the duration map.

    Raises
    ------
    ValueError if no duration can be resolved.
    """

    # If the caller provided an explicit duration, trust it after validation.
    if explicit_duration is not None:
        if explicit_duration <= 0:
            raise ValueError("explicit duration must be greater than zero")
        return explicit_duration

    # If there is no explicit duration and no service name, we cannot infer the
    # requested slot length.
    if not requested_service:
        raise ValueError("Provide either requested_service or explicit_duration")

    # Normalize the service name so formatting differences do not break lookup.
    normalized = normalize_service_name(requested_service)

    # First try exact lookup in the duration map.
    if normalized in DEFAULT_SERVICE_DURATION_MINUTES:
        return DEFAULT_SERVICE_DURATION_MINUTES[normalized]

    # If exact lookup fails, try a softer contains-based match.
    for key, minutes in DEFAULT_SERVICE_DURATION_MINUTES.items():
        if key in normalized or normalized in key:
            return minutes

    # If nothing matches, ask the caller to supply an explicit duration.
    raise ValueError(
        f"No default duration found for service '{requested_service}'. "
        "Pass explicit_duration."
    )


# ---------------------------------------------------------------------------
# Staffing helpers
# ---------------------------------------------------------------------------
def load_staffing_rules(path: Optional[str] = None) -> Dict[int, List[Tuple[str, str, int]]]:
    """
    Load staffing rules from a JSON file, or return defaults.

    Expected JSON shape
    -------------------
    {
      "1": [["07:00", "13:00", 1]],
      "2": [["07:00", "12:00", 1], ["12:00", "17:00", 2]]
    }

    Missing weekdays are treated as closed.
    """

    # If no override file is provided, return the built-in defaults.
    if not path:
        return DEFAULT_STAFFING_RULES

    # Read the JSON override file from disk.
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)

    # Prepare the normalized output structure.
    parsed: Dict[int, List[Tuple[str, str, int]]] = {}

    # Convert string weekday keys into integers and validate each window.
    for weekday_key, windows in raw.items():
        weekday = int(weekday_key)
        normalized_windows: List[Tuple[str, str, int]] = []

        # Validate every staffing window before storing it.
        for window in windows:
            if len(window) != 3:
                raise ValueError(
                    "Each staffing window must be [start_hh:mm, end_hh:mm, workers]"
                )

            # Extract the individual parts of the staffing window.
            start_hhmm, end_hhmm, workers = window

            # Store them in normalized typed form.
            normalized_windows.append((str(start_hhmm), str(end_hhmm), int(workers)))

        # Save the weekday's windows.
        parsed[weekday] = normalized_windows

    # Ensure every weekday exists, even if empty.
    for weekday in range(7):
        parsed.setdefault(weekday, [])

    # Return the normalized staffing rules.
    return parsed



def windows_for_day(
    target_day: date,
    staffing_rules: Dict[int, List[Tuple[str, str, int]]],
) -> List[Tuple[datetime, datetime, int]]:
    """
    Expand staffing rules for one calendar day into datetime windows.
    """

    # Start with an empty list of concrete datetime windows.
    windows: List[Tuple[datetime, datetime, int]] = []

    # Iterate through the configured windows for the requested weekday.
    for start_hhmm, end_hhmm, workers in staffing_rules.get(target_day.weekday(), []):
        # Convert each HH:MM boundary into a concrete datetime on the target day.
        start_dt = combine_local(target_day, start_hhmm)
        end_dt = combine_local(target_day, end_hhmm)

        # Skip invalid or zero-length windows.
        if end_dt <= start_dt:
            continue

        # Keep the validated staffing window.
        windows.append((start_dt, end_dt, workers))

    # Sort windows by start time for deterministic behavior.
    windows.sort(key=lambda item: item[0])

    # Return the concrete staffing windows.
    return windows



def workers_at(
    moment: datetime,
    day_windows: List[Tuple[datetime, datetime, int]],
) -> int:
    """
    Return the total number of groomers available at a moment.

    The implementation sums overlapping windows so future staffing models can
    intentionally stack windows if needed.
    """

    # Start with zero workers.
    total_workers = 0

    # Add the worker count for every staffing window containing this moment.
    for window_start, window_end, worker_count in day_windows:
        if window_start <= moment < window_end:
            total_workers += worker_count

    # Return the total active worker count.
    return total_workers


# ---------------------------------------------------------------------------
# Gingr payload extraction helpers
# ---------------------------------------------------------------------------
def iter_reservations(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """
    Iterate reservation objects from a Gingr payload.

    Gingr payloads in this investigation were seen in two practical shapes:
    - {"error": false, "data": {"123": {...}, "124": {...}}}
    - a plain list[reservation]
    """

    # Prefer the nested data field when it exists.
    data = payload.get("data", payload)

    # If the data container is a dictionary, return its values.
    if isinstance(data, dict):
        return data.values()

    # If the data container is already a list, return it directly.
    if isinstance(data, list):
        return data

    # Otherwise return an empty iterable.
    return []



def service_row_consumes_groomer_capacity(service: Dict[str, Any]) -> bool:
    """
    Decide whether a Gingr service row should count against groomer capacity.

    A service consumes groomer capacity when EITHER:
    - it is assigned to a groomer, OR
    - its service name itself looks grooming-related.

    This is important because real groomer work can appear under boarding,
    daycare, training, or grooming reservations.
    """

    # Extract the service name as text.
    service_name = str(service.get("name") or "")

    # Extract the assigned role as text.
    assigned_to = str(service.get("assigned_to") or "")

    # Return True if either signal suggests real groomer occupancy.
    return assignee_looks_groomer(assigned_to) or service_name_looks_grooming(service_name)



def collect_same_day_grooming_slots(
    payload: Dict[str, Any],
    target_day: date,
) -> List[OccupiedSlot]:
    """
    Extract normalized same-day groomer-occupying service intervals.

    This function intentionally ignores:
    - parent reservation date ranges
    - non-scheduled service rows
    - cancelled reservations
    - non-grooming scheduled services such as trainer sessions or bark ranger
      activities

    This is the most important normalization step in the whole design.
    """

    # Define the local start of the requested day.
    day_start = datetime.combine(target_day, time(0, 0))

    # Define the local start of the following day so we can treat the target day
    # as the half-open interval [day_start, next_day).
    next_day = day_start + timedelta(days=1)

    # Prepare the output list.
    occupied_slots: List[OccupiedSlot] = []

    # Walk through every reservation returned by Gingr.
    for reservation in iter_reservations(payload):
        # Skip any malformed items defensively.
        if not isinstance(reservation, dict):
            continue

        # Cancelled parent reservations should not block future availability.
        if reservation.get("cancelled_date"):
            continue

        # Extract reservation-level context used for debugging and logging.
        reservation_id = str(reservation.get("reservation_id") or reservation.get("id") or "")
        reservation_type = reservation.get("reservation_type") or {}
        reservation_type_id = str(reservation_type.get("id") or "")
        reservation_type_name = str(
            reservation_type.get("type")
            or reservation_type.get("name")
            or ""
        )
        animal = reservation.get("animal") or {}
        animal_name = str(animal.get("name") or "")

        # Walk through every service row inside the reservation.
        for service in reservation.get("services") or []:
            # Skip malformed service rows defensively.
            if not isinstance(service, dict):
                continue

            # Pull the scheduled boundaries.
            scheduled_at = service.get("scheduled_at")
            scheduled_until = service.get("scheduled_until")

            # Only scheduled services can consume timed grooming capacity.
            if not scheduled_at or not scheduled_until:
                continue

            # Skip service rows that do not look like groomer work.
            if not service_row_consumes_groomer_capacity(service):
                continue

            # Parse the scheduled boundaries into local datetimes.
            service_start = parse_local_iso(str(scheduled_at))
            service_end = parse_local_iso(str(scheduled_until))

            # Ignore broken or zero-length intervals.
            if service_end <= service_start:
                continue

            # Only keep service intervals that overlap the requested calendar day.
            if not overlaps(service_start, service_end, day_start, next_day):
                continue

            # Clip the service interval to the target day boundaries.
            clipped_start = max(service_start, day_start)
            clipped_end = min(service_end, next_day)

            # Store the normalized occupied interval.
            occupied_slots.append(
                OccupiedSlot(
                    reservation_id=reservation_id,
                    reservation_type_id=reservation_type_id,
                    reservation_type_name=reservation_type_name,
                    animal_name=animal_name,
                    service_id=str(service.get("id") or ""),
                    service_name=str(service.get("name") or ""),
                    assigned_to=service.get("assigned_to"),
                    start=clipped_start.isoformat(timespec="minutes"),
                    end=clipped_end.isoformat(timespec="minutes"),
                )
            )

    # Sort results to keep output stable and human-readable.
    occupied_slots.sort(key=lambda slot: (slot.start, slot.end, slot.service_name, slot.animal_name))

    # Return the normalized same-day occupied slots.
    return occupied_slots


# ---------------------------------------------------------------------------
# Slot checking helpers
# ---------------------------------------------------------------------------
def build_segments(
    requested_start: datetime,
    requested_end: datetime,
    occupied_slots: List[OccupiedSlot],
    day_windows: List[Tuple[datetime, datetime, int]],
) -> List[Tuple[datetime, datetime]]:
    """
    Split a requested slot into smaller segments at all relevant boundaries.

    Relevant boundaries include:
    - requested slot start/end
    - occupied booking start/end points that overlap the request
    - staffing window boundaries that overlap the request

    Why segmenting matters
    ----------------------
    A single requested interval may cross:
    - from available time into unavailable time
    - from 1 groomer to 2 groomers
    - from no overlap into an overlap

    Segmenting lets the checker evaluate those changes precisely.
    """

    # Start with the requested interval boundaries themselves.
    boundary_points = {requested_start, requested_end}

    # Add occupied-slot boundaries that overlap the requested interval.
    for occupied in occupied_slots:
        occupied_start = datetime.fromisoformat(occupied.start)
        occupied_end = datetime.fromisoformat(occupied.end)

        if overlaps(occupied_start, occupied_end, requested_start, requested_end):
            boundary_points.add(max(occupied_start, requested_start))
            boundary_points.add(min(occupied_end, requested_end))

    # Add staffing-window boundaries that overlap the requested interval.
    for window_start, window_end, _worker_count in day_windows:
        if overlaps(window_start, window_end, requested_start, requested_end):
            boundary_points.add(max(window_start, requested_start))
            boundary_points.add(min(window_end, requested_end))

    # Sort all boundaries into chronological order.
    sorted_points = sorted(boundary_points)

    # Convert adjacent boundary pairs into concrete segments.
    segments: List[Tuple[datetime, datetime]] = []
    for index in range(len(sorted_points) - 1):
        segment_start = sorted_points[index]
        segment_end = sorted_points[index + 1]

        if segment_start < segment_end:
            segments.append((segment_start, segment_end))

    # Return the request-specific segments.
    return segments



def check_slot(
    requested_start: datetime,
    requested_end: datetime,
    occupied_slots: List[OccupiedSlot],
    day_windows: List[Tuple[datetime, datetime, int]],
) -> Tuple[bool, str, List[SegmentCheck]]:
    """
    Check whether a requested slot is available.

    Return value
    ------------
    (available, reason, segment_checks)

    Reason values
    -------------
    - "available"
    - "outside_staffing_hours"
    - "groomer_capacity_full"
    - "invalid_slot_range"
    """

    # Reject zero-length or backwards intervals immediately.
    if requested_end <= requested_start:
        return False, "invalid_slot_range", []

    # Build the smallest meaningful segments for this request.
    segments = build_segments(requested_start, requested_end, occupied_slots, day_windows)

    # Keep a detailed trace of how every segment was evaluated.
    segment_checks: List[SegmentCheck] = []

    # If segmentation produced nothing, treat it as an invalid slot.
    if not segments:
        return False, "invalid_slot_range", []

    # Evaluate each segment independently.
    for segment_start, segment_end in segments:
        # Determine how many groomers are working at the segment start.
        worker_count = workers_at(segment_start, day_windows)

        # Count how many existing occupied slots overlap this segment.
        overlapping_bookings = 0
        for occupied in occupied_slots:
            occupied_start = datetime.fromisoformat(occupied.start)
            occupied_end = datetime.fromisoformat(occupied.end)

            if overlaps(occupied_start, occupied_end, segment_start, segment_end):
                overlapping_bookings += 1

        # The segment is acceptable only if workers are present and the number
        # of overlapping bookings is strictly less than the number of workers.
        segment_ok = worker_count > 0 and overlapping_bookings < worker_count

        # Record the detailed evaluation for debugging.
        segment_checks.append(
            SegmentCheck(
                start=segment_start.isoformat(timespec="minutes"),
                end=segment_end.isoformat(timespec="minutes"),
                workers=worker_count,
                overlapping_bookings=overlapping_bookings,
                ok=segment_ok,
            )
        )

        # If this segment failed because there are no workers, return the
        # staffing-hours reason immediately.
        if not segment_ok and worker_count <= 0:
            return False, "outside_staffing_hours", segment_checks

        # If this segment failed despite workers being present, capacity is full.
        if not segment_ok:
            return False, "groomer_capacity_full", segment_checks

    # If every segment passed, the full request is available.
    return True, "available", segment_checks



def find_next_available_start(
    start_day: date,
    requested_start: datetime,
    duration_minutes: int,
    occupied_by_day: Dict[date, List[OccupiedSlot]],
    staffing_rules: Dict[int, List[Tuple[str, str, int]]],
    lookahead_days: int,
    step_minutes: int,
) -> Optional[datetime]:
    """
    Find the next available start time at or after the requested start.

    The search walks day by day, then scans candidate starts in fixed increments.
    """

    # Convert the requested duration into a timedelta once.
    requested_duration = timedelta(minutes=duration_minutes)

    # Search from the requested day through the configured lookahead horizon.
    for offset in range(0, lookahead_days + 1):
        current_day = start_day + timedelta(days=offset)

        # Get the staffing windows for this day.
        day_windows = windows_for_day(current_day, staffing_rules)

        # Closed days cannot contain an available slot.
        if not day_windows:
            continue

        # Get the already-occupied same-day grooming slots for this day.
        occupied_slots = occupied_by_day.get(current_day, [])

        # Determine the first and last possible moments inside staffing windows.
        day_open = min(window_start for window_start, _window_end, _workers in day_windows)
        day_close = max(window_end for _window_start, window_end, _workers in day_windows)

        # Start the cursor at the day's opening time rounded up to the search step.
        cursor = round_up_to_step(day_open, step_minutes)

        # On the first day only, do not search before the originally requested
        # start time.
        if offset == 0 and requested_start > cursor:
            cursor = round_up_to_step(requested_start, step_minutes)

        # The latest valid start is the day closing time minus the requested duration.
        latest_start = day_close - requested_duration

        # Scan candidate starts across the day.
        while cursor <= latest_start:
            candidate_end = cursor + requested_duration
            slot_available, _reason, _checks = check_slot(
                cursor,
                candidate_end,
                occupied_slots,
                day_windows,
            )

            if slot_available:
                return cursor

            cursor += timedelta(minutes=step_minutes)

    # If no valid start was found within the lookahead window, return None.
    return None


# ---------------------------------------------------------------------------
# Gingr API access helpers
# ---------------------------------------------------------------------------
def fetch_reservations_for_day_from_api(
    target_day: date,
    location_id: str,
) -> Dict[str, Any]:
    """
    Fetch a single day's reservations from Gingr using the reservations endpoint.

    This is the authoritative live-data source used by the checker.
    """

    # Read env vars at call time (not at import time) so that load_dotenv()
    # called after module import — or in a subprocess worker — is respected.
    api_key = os.environ.get("GINGR_API_KEY", "").strip().strip('"').strip("'")
    tenant = os.environ.get("GINGR_TENANT", "happyhound").strip().strip('"').strip("'")
    api_base = os.environ.get(
        "GINGR_API_BASE",
        f"https://{tenant}.gingrapp.com/api/v1",
    ).strip().strip('"').strip("'")

    # Refuse to continue if the API key is missing.
    if not api_key:
        raise RuntimeError("GINGR_API_KEY is not set")

    # Build the full reservations endpoint URL.
    url = f"{api_base.rstrip('/')}/reservations"

    # Prepare the form-encoded POST body expected by this Gingr endpoint.
    body = {
        "key": api_key,
        "checked_in": "false",
        "start_date": target_day.isoformat(),
        "end_date": target_day.isoformat(),
        "location_id": location_id,
    }

    # Perform the HTTP POST request.
    response = requests.post(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        timeout=60,
    )

    # Raise for any non-2xx HTTP result.
    response.raise_for_status()

    # Parse the JSON response body.
    payload = response.json()

    # Gingr can also signal application-level errors via the payload body.
    if isinstance(payload, dict) and payload.get("error") is True:
        raise RuntimeError(f"Gingr API returned error payload: {payload}")

    # Return the parsed payload.
    return payload



def load_payload_from_file(path: str) -> Dict[str, Any]:
    """
    Load a previously saved reservations payload from local disk.

    This is useful for testing and replaying known examples without making
    live API calls.
    """

    # Open the local JSON file.
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    # Return the parsed payload.
    return payload


# ---------------------------------------------------------------------------
# Core grooming availability checker
# ---------------------------------------------------------------------------
def check_grooming_availability(
    target_day: date,
    requested_start_hhmm: str,
    requested_service: Optional[str],
    duration_minutes: int,
    location_id: str,
    staffing_rules: Dict[int, List[Tuple[str, str, int]]],
    lookahead_days: int,
    step_minutes: int,
    payload_by_day: Optional[Dict[date, Dict[str, Any]]] = None,
) -> AvailabilityResult:
    """
    Check grooming availability for a requested date/time/service.

    Parameters
    ----------
    target_day:
        Calendar date being requested.
    requested_start_hhmm:
        Requested start time in HH:MM local business time.
    requested_service:
        Human-readable service name, used only for result reporting here.
    duration_minutes:
        Slot duration to reserve.
    location_id:
        Gingr location ID.
    staffing_rules:
        Groomer staffing windows.
    lookahead_days:
        How many days to search forward for the next available start.
    step_minutes:
        Increment used while searching for next available start.
    payload_by_day:
        Optional preloaded payloads keyed by date for offline replay.

    Returns
    -------
    AvailabilityResult
    """

    # This dictionary will hold normalized occupied grooming slots for every day
    # we inspect while searching availability and next-available fallback times.
    occupied_by_day: Dict[date, List[OccupiedSlot]] = {}

    # Ensure we have a usable dictionary for offline payload replay mode.
    payload_by_day = payload_by_day or {}

    # Gather data for the requested day and every lookahead day.
    for offset in range(0, lookahead_days + 1):
        current_day = target_day + timedelta(days=offset)

        # If a payload for this day was explicitly supplied, reuse it.
        if current_day in payload_by_day:
            payload = payload_by_day[current_day]
        else:
            # Otherwise fetch the live payload from Gingr.
            payload = fetch_reservations_for_day_from_api(current_day, location_id)

        # Normalize this day's same-day groomer-occupying service intervals.
        occupied_by_day[current_day] = collect_same_day_grooming_slots(payload, current_day)

    # Convert the requested slot boundaries into concrete datetimes.
    requested_start = combine_local(target_day, requested_start_hhmm)
    requested_end = requested_start + timedelta(minutes=duration_minutes)

    # Expand the day's staffing rules into concrete windows.
    day_windows = windows_for_day(target_day, staffing_rules)

    # Check whether the requested slot itself is available.
    available, reason, segment_checks = check_slot(
        requested_start,
        requested_end,
        occupied_by_day[target_day],
        day_windows,
    )

    # Pre-fill the next-available field as unknown.
    next_available_start: Optional[datetime] = None

    # Only search for another slot when the original request is unavailable.
    if not available:
        next_available_start = find_next_available_start(
            start_day=target_day,
            requested_start=requested_start,
            duration_minutes=duration_minutes,
            occupied_by_day=occupied_by_day,
            staffing_rules=staffing_rules,
            lookahead_days=lookahead_days,
            step_minutes=step_minutes,
        )

    # Build and return the structured result object.
    return AvailabilityResult(
        category="GROOMING",
        requested_service=requested_service,
        date=target_day.isoformat(),
        requested_start=requested_start.isoformat(timespec="minutes"),
        requested_end=requested_end.isoformat(timespec="minutes"),
        duration_minutes=duration_minutes,
        available=available,
        reason=reason,
        next_available_start=(
            next_available_start.isoformat(timespec="minutes")
            if next_available_start is not None
            else None
        ),
        location_id=location_id,
        occupied_slots=[asdict(slot) for slot in occupied_by_day[target_day]],
        segment_checks=[asdict(check) for check in segment_checks],
    )


# ---------------------------------------------------------------------------
# Agent-facing orchestration function
# ---------------------------------------------------------------------------
def determine_service_availability(
    category: str,
    requested_date: str,
    requested_start_hhmm: str,
    requested_service: Optional[str] = None,
    explicit_duration: Optional[int] = None,
    location_id: Optional[str] = None,
    staffing_rules: Optional[Dict[int, List[Tuple[str, str, int]]]] = None,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    step_minutes: int = DEFAULT_STEP_MINUTES,
    payload_by_day: Optional[Dict[date, Dict[str, Any]]] = None,
) -> AvailabilityResult:
    """
    Main entry point for agent integration.

    This function enforces the final business decision:
    - daycare and boarding default to available
    - grooming and grooming-like add-ons use live slot logic

    Examples
    --------
    - category="Daycare", service=None
        -> returns available=True immediately
    - category="Boarding", service="Deluxe Bath"
        -> uses grooming availability logic
    - category="Grooming", service="Mini Groom"
        -> uses grooming availability logic
    """

    # If no staffing rules were provided, use the defaults.
    staffing_rules = staffing_rules or DEFAULT_STAFFING_RULES

    # Resolve location_id at call time so subprocess workers with freshly
    # loaded env vars are not stuck with the import-time empty default.
    if location_id is None:
        location_id = os.environ.get("GINGR_LOCATION_ID", "1").strip().strip('"').strip("'")

    # If the request does NOT require grooming logic, short-circuit to available.
    if not request_requires_grooming_logic(category, requested_service):
        return AvailabilityResult(
            category=category,
            requested_service=requested_service,
            date=requested_date,
            requested_start=None,
            requested_end=None,
            duration_minutes=explicit_duration,
            available=True,
            reason="non_grooming_baked_in_available",
            next_available_start=None,
            location_id=str(location_id),
            occupied_slots=[],
            segment_checks=[],
        )

    # Parse the requested date once.
    target_day = parse_date(requested_date)

    # Resolve the requested slot duration.
    duration_minutes = resolve_duration_minutes(requested_service, explicit_duration)

    # Delegate to the real grooming checker.
    return check_grooming_availability(
        target_day=target_day,
        requested_start_hhmm=requested_start_hhmm,
        requested_service=requested_service,
        duration_minutes=duration_minutes,
        location_id=str(location_id),
        staffing_rules=staffing_rules,
        lookahead_days=lookahead_days,
        step_minutes=step_minutes,
        payload_by_day=payload_by_day,
    )


# ---------------------------------------------------------------------------
# Command-line helpers
# ---------------------------------------------------------------------------
def build_payload_map_from_cli(args: argparse.Namespace) -> Dict[date, Dict[str, Any]]:
    """
    Build an optional payload map from command-line file arguments.

    Supported usage
    ---------------
    --payload-file YYYY-MM-DD=path/to/file.json

    This can be supplied multiple times.
    """

    # Prepare the output map.
    payload_map: Dict[date, Dict[str, Any]] = {}

    # If no payload-file arguments were passed, return an empty map.
    if not args.payload_file:
        return payload_map

    # Parse each KEY=VALUE argument pair.
    for item in args.payload_file:
        if "=" not in item:
            raise ValueError(
                "Each --payload-file must look like YYYY-MM-DD=path/to/file.json"
            )

        # Split the date from the file path.
        day_text, file_path = item.split("=", 1)

        # Parse the date key.
        day_value = parse_date(day_text)

        # Load the file and store it under that date.
        payload_map[day_value] = load_payload_from_file(file_path)

    # Return the final payload map.
    return payload_map



def cli() -> None:
    """
    Command-line interface for manual testing and local validation.
    """

    # Create the top-level argument parser.
    parser = argparse.ArgumentParser(
        description="Happy Hound grooming-aware availability checker"
    )

    # Add the top-level reservation category argument.
    parser.add_argument(
        "--category",
        required=True,
        help="Examples: Daycare, Boarding, Grooming, Training",
    )

    # Add the requested date argument.
    parser.add_argument(
        "--date",
        required=True,
        help="Requested service date in YYYY-MM-DD",
    )

    # Add the requested local start time argument.
    parser.add_argument(
        "--start",
        required=True,
        help="Requested local start time in HH:MM",
    )

    # Add the optional service name argument.
    parser.add_argument(
        "--service",
        help='Requested service name, e.g. "Mini Groom" or "Deluxe Bath"',
    )

    # Add the optional explicit duration override argument.
    parser.add_argument(
        "--duration",
        type=int,
        help="Explicit requested duration in minutes",
    )

    # Add the location override argument.
    parser.add_argument(
        "--location-id",
        default=GINGR_LOCATION_ID,
        help="Gingr location ID",
    )

    # Add an optional staffing override file argument.
    parser.add_argument(
        "--staffing-file",
        help="Optional JSON staffing rules override",
    )

    # Add the lookahead-days argument.
    parser.add_argument(
        "--lookahead-days",
        type=int,
        default=DEFAULT_LOOKAHEAD_DAYS,
        help="Days to search ahead for next available start",
    )

    # Add the search step size argument.
    parser.add_argument(
        "--step-minutes",
        type=int,
        default=DEFAULT_STEP_MINUTES,
        help="Increment used while searching for next available slot",
    )

    # Add optional local payload replay arguments.
    parser.add_argument(
        "--payload-file",
        action="append",
        help="Offline replay input in the form YYYY-MM-DD=path/to/file.json",
    )

    # Parse all command-line arguments.
    args = parser.parse_args()

    try:
        # Load staffing rules, either defaults or an override file.
        staffing_rules = load_staffing_rules(args.staffing_file)

        # Load any optional local payload replay files.
        payload_by_day = build_payload_map_from_cli(args)

        # Run the main orchestration function.
        result = determine_service_availability(
            category=args.category,
            requested_date=args.date,
            requested_start_hhmm=args.start,
            requested_service=args.service,
            explicit_duration=args.duration,
            location_id=str(args.location_id),
            staffing_rules=staffing_rules,
            lookahead_days=int(args.lookahead_days),
            step_minutes=int(args.step_minutes),
            payload_by_day=payload_by_day,
        )

        # Print the result as pretty JSON for logs and manual inspection.
        print(json.dumps(asdict(result), indent=2, ensure_ascii=False))

    except requests.HTTPError as error:
        fail(f"HTTP error calling Gingr: {error}", 2)
    except Exception as error:
        fail(f"{type(error).__name__}: {error}", 3)


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Invoke the command-line interface when the file is executed directly.
    cli()
