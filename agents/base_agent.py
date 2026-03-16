"""Base agent with shared handoff logic."""
import logging
from dataclasses import dataclass, field
from typing import Optional

from livekit.agents.llm import ChatContext
from livekit.agents.voice import Agent, RunContext

logger = logging.getLogger("doheny-surf-desk")


@dataclass
class SurfBookingData:
    """Session data for Happy Hound booking workflow."""

    # Customer profile
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    dog_weight_lbs: Optional[float] = None
    dog_size: Optional[str] = None  # small | medium | large | x-large

    # Booking request
    requested_services: list[str] = field(default_factory=list)
    service_family: Optional[str] = None
    service_plan: Optional[str] = None
    selection_source: Optional[str] = None
    requested_date: Optional[str] = None
    requested_time: Optional[str] = None
    booking_id: Optional[str] = None
    instructor_name: Optional[str] = None

    # Billing / quote state
    quoted_subtotal: Optional[float] = None
    quoted_tax: Optional[float] = None
    quoted_total: Optional[float] = None
    quote_notes: Optional[str] = None
    handoff_status: Optional[str] = None  # pending | sent | failed
    handoff_pending_action: Optional[str] = None
    runtime_tool_facts: dict = field(default_factory=dict)
    session_trace_id: Optional[str] = None

    # Legacy fields retained for backward compatibility (currently not in intake path)
    age: Optional[int] = None
    experience_level: Optional[str] = None
    preferred_date: Optional[str] = None
    preferred_time: Optional[str] = None
    spot_location: Optional[str] = None
    height_cm: Optional[int] = None
    weight_kg: Optional[int] = None
    board_size: Optional[str] = None
    wetsuit_size: Optional[str] = None
    accessories: list = field(default_factory=list)
    payment_status: Optional[str] = None
    total_amount: Optional[float] = 0.0
    is_minor: bool = False
    has_injury: bool = False
    guardian_consent: Optional[bool] = None
    guardian_name: Optional[str] = None
    guardian_contact: Optional[str] = None

    # Agent registry for returning to frontdesk
    personas: dict = field(default_factory=dict)

    def is_profile_complete(self) -> bool:
        """Check if basic profile is complete."""
        return all(
            [
                self.name,
                self.phone,
                self.dog_weight_lbs is not None,
                self.dog_size,
            ]
        )

    def is_booking_complete(self) -> bool:
        """Check if booking request details are complete."""
        return all(
            [
                self.service_family or self.requested_services,
                self.requested_date,
                self.requested_time,
            ]
        )

    def is_gear_selected(self) -> bool:
        """Check if legacy gear selection data is complete."""
        return all(
            [
                self.board_size,
                self.wetsuit_size,
            ]
        )

    def summarize(self) -> str:
        """Return a summary of current session state."""
        parts = []

        if self.name:
            parts.append(f"Customer: {self.name}")
        if self.phone:
            parts.append(f"Phone: {self.phone}")
        if self.dog_weight_lbs is not None:
            parts.append(f"Dog Weight: {self.dog_weight_lbs:.1f} lbs")
        if self.dog_size:
            parts.append(f"Dog Size: {self.dog_size}")
        if self.requested_services:
            parts.append(f"Services: {', '.join(self.requested_services)}")
        if self.service_family:
            parts.append(f"Service Family: {self.service_family}")
        if self.service_plan:
            parts.append(f"Service Plan: {self.service_plan}")
        if self.requested_date and self.requested_time:
            parts.append(f"Requested: {self.requested_date} at {self.requested_time}")
        if self.quoted_total is not None:
            parts.append(f"Quoted Total: ${self.quoted_total:.2f}")
        if self.handoff_status:
            parts.append(f"Handoff: {self.handoff_status}")

        return " | ".join(parts) if parts else "No booking info yet"


RunContext_T = RunContext[SurfBookingData]


class BaseAgent(Agent):
    """Base agent with shared handoff logic."""

    def __init__(self, chat_ctx: Optional[ChatContext] = None, **kwargs):
        super().__init__(chat_ctx=chat_ctx, **kwargs)
