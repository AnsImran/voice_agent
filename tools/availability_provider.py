"""Availability provider boundary and mock implementation for Happy Hound."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


SERVICE_ALIASES = {
    "daycare": "daycare",
    "day care": "daycare",
    "drop-in": "daycare",
    "drop in": "daycare",
    "day visit": "daycare",
    "boarding": "boarding",
    "sleepover": "boarding",
    "overnight": "boarding",
    "grooming": "grooming",
    "groom": "grooming",
    "bath": "grooming",
    "training": "training",
    "train": "training",
    "a-la-bark": "training",
}

SERVICE_PLAN_ALIASES = {
    "golden leash club card": "golden_leash_club",
    "golden leash club": "golden_leash_club",
    "golden leash": "golden_leash_club",
    "golden leaf club card": "golden_leash_club",
    "golden leaf club": "golden_leash_club",
    "golden lease club card": "golden_leash_club",
    "golden issue club card": "golden_leash_club",
}

SERVICE_PLAN_META = {
    "golden_leash_club": {
        "family": "daycare",
        "label": "Golden Leash Club Card",
        "subtotal": 775.0,
        "billing_cycle": "monthly",
        "quote_notes": "Unlimited daycare plan with 6-month commitment.",
    }
}

SERVICE_META = {
    "daycare": {
        "label": "Daycare (Drop-in Day Visit)",
        "duration": "all day",
        "times": ["07:00", "08:00", "09:00", "12:00", "14:00"],
        "staff": ["Alex", "Brooke", "Diego", "Priya"],
    },
    "boarding": {
        "label": "Classic Sleepover",
        "duration": "overnight",
        "times": ["10:00", "12:00", "14:00", "16:00"],
        "staff": ["Janelle", "Mateo", "Chris"],
    },
    "grooming": {
        "label": "Basic Bath",
        "duration": "60-90 minutes",
        "times": ["09:00", "11:00", "13:00", "15:00"],
        "staff": ["Taylor", "Morgan", "Sam"],
    },
    "training": {
        "label": "A-La-Bark",
        "duration": "1 hour",
        "times": ["10:00", "12:00", "14:00", "16:00"],
        "staff": ["Jordan", "Riley"],
    },
}

BASE_PRICING = {
    "daycare": 70.0,
    "boarding": 125.0,
    "training": 100.0,
}

GROOMING_PRICING_BY_SIZE = {
    "small": 55.0,
    "medium": 70.0,
    "large": 90.0,
    "x-large": 90.0,
}


@dataclass
class AvailabilitySlot:
    """Provider-normalized availability slot."""

    service: str
    service_label: str
    date: str
    time: str
    staff: str
    duration: str
    price: float


class AvailabilityProvider(Protocol):
    """Provider interface used by SchedulerAgent."""

    def get_slots(
        self,
        service: str,
        date: str,
        time_preference: str,
        dog_size: str | None,
    ) -> list[AvailabilitySlot]:
        """Return service slots for date and preference."""


def normalize_service_plan(value: str | None) -> str | None:
    """Normalize package/plan phrases to canonical plan keys."""
    if not value:
        return None
    lowered = value.strip().lower()
    if lowered in SERVICE_PLAN_ALIASES:
        return SERVICE_PLAN_ALIASES[lowered]
    for alias, canonical in SERVICE_PLAN_ALIASES.items():
        if alias in lowered:
            return canonical
    return None


def normalize_service(service: str | None) -> str:
    """Normalize caller phrasing to canonical service key."""
    if not service:
        return "daycare"
    value = service.strip().lower()
    if value in SERVICE_ALIASES:
        return SERVICE_ALIASES[value]

    for alias, normalized in SERVICE_ALIASES.items():
        if alias in value:
            return normalized
    return "daycare"


def resolve_service_selection(
    value: str | None,
    existing_family: str | None = None,
    existing_plan: str | None = None,
) -> tuple[str, str | None]:
    """Resolve service family + package plan from a user/tool value."""
    plan = normalize_service_plan(value)
    if plan:
        return SERVICE_PLAN_META[plan]["family"], plan

    text = (value or "").strip().lower()
    family = normalize_service(value or existing_family)

    # Explicit drop-in/day-visit wording means no package plan.
    if any(token in text for token in ("drop-in", "drop in", "day visit", "simple daycare")):
        return "daycare", None

    # If caller references only the family and an existing plan matches, preserve plan.
    if existing_plan and SERVICE_PLAN_META[existing_plan]["family"] == family:
        if not text or family in text:
            return family, existing_plan

    return family, None


def get_service_display_label(service_family: str, service_plan: str | None) -> str:
    """Return user-facing service label."""
    if service_plan and service_plan in SERVICE_PLAN_META:
        return str(SERVICE_PLAN_META[service_plan]["label"])
    return str(SERVICE_META[service_family]["label"])


def compute_selection_quote(
    service_family: str,
    service_plan: str | None,
    dog_size: str | None,
) -> dict[str, str | float]:
    """Compute quote based on selected family/plan."""
    if service_plan and service_plan in SERVICE_PLAN_META:
        plan_meta = SERVICE_PLAN_META[service_plan]
        subtotal = float(plan_meta["subtotal"])
        tax = 0.0
        total = subtotal + tax
        return {
            "label": str(plan_meta["label"]),
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
            "billing_cycle": str(plan_meta["billing_cycle"]),
            "quote_notes": str(plan_meta["quote_notes"]),
        }

    subtotal = _compute_price(service_family, dog_size=dog_size)
    tax = 0.0
    total = subtotal + tax
    return {
        "label": get_service_display_label(service_family, service_plan=None),
        "subtotal": float(subtotal),
        "tax": tax,
        "total": float(total),
        "billing_cycle": "per_visit",
        "quote_notes": "Standard service pricing.",
    }


def _compute_price(service: str, dog_size: str | None) -> float:
    if service == "grooming":
        return GROOMING_PRICING_BY_SIZE.get((dog_size or "").lower(), 70.0)
    return BASE_PRICING[service]


def _filter_times(times: list[str], time_preference: str) -> list[str]:
    pref = (time_preference or "anytime").lower().strip()
    if not pref or pref in {"any", "anytime"}:
        return times

    if "morning" in pref:
        filtered = [slot for slot in times if int(slot.split(":")[0]) < 12]
        return filtered or times

    if "afternoon" in pref or "evening" in pref:
        filtered = [slot for slot in times if int(slot.split(":")[0]) >= 12]
        return filtered or times

    digits = "".join(ch for ch in pref if ch.isdigit())
    if digits:
        hour = int(digits[:2]) if len(digits) >= 2 else int(digits[0])
        filtered = [slot for slot in times if int(slot.split(":")[0]) == hour]
        return filtered or times

    return times


class MockAvailabilityProvider:
    """Deterministic mock provider used until real API integration."""

    def get_slots(
        self,
        service: str,
        date: str,
        time_preference: str,
        dog_size: str | None,
    ) -> list[AvailabilitySlot]:
        normalized_service = normalize_service(service)
        meta = SERVICE_META[normalized_service]
        price = _compute_price(normalized_service, dog_size=dog_size)

        selected_times = _filter_times(meta["times"], time_preference=time_preference)
        seed = int(
            hashlib.sha256(
                f"{normalized_service}|{date}|{time_preference}".encode("utf-8")
            ).hexdigest()[:8],
            16,
        )
        offset = seed % len(meta["staff"])

        slots: list[AvailabilitySlot] = []
        for idx, time_value in enumerate(selected_times[:3]):
            staff = meta["staff"][(idx + offset) % len(meta["staff"])]
            slots.append(
                AvailabilitySlot(
                    service=normalized_service,
                    service_label=meta["label"],
                    date=date,
                    time=time_value,
                    staff=staff,
                    duration=meta["duration"],
                    price=price,
                )
            )
        return slots
