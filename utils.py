"""Utility functions for Happy Hound agent workflow."""
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
import zoneinfo

import yaml

DEFAULT_AGENT_TTS = {
    # Alternating pattern for active handoff chain:
    # FrontDesk (female) -> Scheduler (male via SESSION_TTS) -> Intake (female) -> Billing (male)
    "frontdesk": "deepgram/aura-2:andromeda",
    "intake": "deepgram/aura-2:amalthea",
    "billing": "deepgram/aura-2:zeus",
}


def load_reading_guidelines() -> str:
    """Load reading guidelines from YAML file.

    Returns:
        Reading guidelines text, or empty string if file not found
    """
    guidelines_path = Path(__file__).parent / "prompts" / "reading_guidelines.yaml"
    if guidelines_path.exists():
        with open(guidelines_path, 'r') as f:
            guidelines_data = yaml.safe_load(f)
            return guidelines_data.get('prompt', '')
    return ''


def load_business_facts() -> str:
    """Load shared Happy Hound business facts from YAML file.

    Returns:
        Business facts text, or empty string if file not found
    """
    facts_path = Path(__file__).parent / "prompts" / "business_facts.yaml"
    if facts_path.exists():
        with open(facts_path, 'r') as f:
            facts_data = yaml.safe_load(f)
            return facts_data.get('prompt', '')
    return ''


def get_current_date() -> str:
    """Get current date and day of week for California/Los Angeles timezone.
    
    Returns:
        Single line with day of week and date
    """
    try:
        pacific_tz = zoneinfo.ZoneInfo("America/Los_Angeles")
        now = datetime.now(pacific_tz)
        return now.strftime("%A, %B %d, %Y")
    except Exception:
        now = datetime.utcnow()
        return now.strftime("%A, %B %d, %Y")


def load_prompt(
    filename: str,
    include_reading_guidelines: bool = True,
    include_business_facts: bool = False,
    **variables,
) -> str:
    """Load a prompt from a YAML file with variable substitution.

    Args:
        filename: Name of the YAML file (e.g., 'scheduler_prompt.yaml')
        include_reading_guidelines: If True, prepend reading guidelines
        include_business_facts: If True, prepend shared Happy Hound business facts
        **variables: Variables to substitute in the prompt (e.g., current_date="...")

    Returns:
        The prompt text with variables substituted
    """
    prompt_path = Path(__file__).parent / "prompts" / filename
    with open(prompt_path, 'r') as f:
        data = yaml.safe_load(f)
        prompt_text = data.get('prompt', '')

    if variables:
        prompt_text = prompt_text.format(**variables)

    if include_business_facts:
        facts = load_business_facts()
        if facts:
            prompt_text = f"{facts}\n\n{'-' * 50}\n\n{prompt_text}"

    if include_reading_guidelines:
        guidelines = load_reading_guidelines()
        if guidelines:
            prompt_text = f"{guidelines}\n\n{'-' * 50}\n\n{prompt_text}"

    return prompt_text


def parse_env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean environment flag."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def resolve_agent_tts(agent_name: str) -> str | None:
    """Resolve a per-agent TTS descriptor from environment variables.

    Expected value format:
      provider/model:voice_id
    Example:
      cartesia/sonic-3:f786b574-daa5-4673-aa0c-cbe3e8534c02
    """
    normalized = agent_name.strip().upper().replace("-", "_")
    candidates = [
        f"HH_TTS_{normalized}",
        f"{normalized}_TTS",
    ]
    for key in candidates:
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
    return DEFAULT_AGENT_TTS.get(agent_name.strip().lower())


def ensure_session_trace_id(userdata) -> str:
    """Ensure a stable per-session correlation id exists on userdata."""
    trace_id = getattr(userdata, "session_trace_id", None)
    if not trace_id:
        trace_id = f"hh-{uuid.uuid4().hex[:10]}"
        setattr(userdata, "session_trace_id", trace_id)
    return trace_id


def format_trace_payload(payload: dict) -> str:
    """Format trace payload safely for log lines."""
    try:
        return json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:
        return str(payload)


def trace_log(logger, flag_name: str, trace_id: str, message: str, **payload) -> None:
    """Emit env-gated trace logs with a correlation id."""
    if not parse_env_bool(flag_name, default=False):
        return
    if payload:
        logger.info("[TRACE][%s] %s %s", trace_id, message, format_trace_payload(payload))
    else:
        logger.info("[TRACE][%s] %s", trace_id, message)


def userdata_snapshot(userdata) -> dict:
    """Capture a compact state snapshot for debugging diffs."""
    return {
        "name": getattr(userdata, "name", None),
        "phone": getattr(userdata, "phone", None),
        "dog_weight_lbs": getattr(userdata, "dog_weight_lbs", None),
        "dog_size": getattr(userdata, "dog_size", None),
        "service_family": getattr(userdata, "service_family", None),
        "service_plan": getattr(userdata, "service_plan", None),
        "requested_services": list(getattr(userdata, "requested_services", []) or []),
        "requested_date": getattr(userdata, "requested_date", None),
        "requested_time": getattr(userdata, "requested_time", None),
        "booking_id": getattr(userdata, "booking_id", None),
        "quoted_subtotal": getattr(userdata, "quoted_subtotal", None),
        "quoted_tax": getattr(userdata, "quoted_tax", None),
        "quoted_total": getattr(userdata, "quoted_total", None),
        "quote_notes": getattr(userdata, "quote_notes", None),
        "handoff_status": getattr(userdata, "handoff_status", None),
        "handoff_pending_action": getattr(userdata, "handoff_pending_action", None),
    }


def userdata_diff(before: dict, after: dict) -> dict:
    """Return changed fields between two snapshots."""
    changes: dict = {}
    keys = set(before.keys()) | set(after.keys())
    for key in sorted(keys):
        old_val = before.get(key)
        new_val = after.get(key)
        if old_val != new_val:
            changes[key] = {"before": old_val, "after": new_val}
    return changes


def format_booking_summary(userdata) -> str:
    """Format booking information into a readable summary.
    
    Args:
        userdata: SurfBookingData instance
        
    Returns:
        Formatted booking summary string
    """
    lines = []
    
    if userdata.name:
        lines.append(f"Name: {userdata.name}")
    if userdata.email:
        lines.append(f"Email: {userdata.email}")
    if userdata.phone:
        lines.append(f"Phone: {userdata.phone}")
    if userdata.age:
        lines.append(f"Age: {userdata.age}")
    if userdata.experience_level:
        lines.append(f"Experience: {userdata.experience_level}")
    if userdata.preferred_date:
        lines.append(f"Date: {userdata.preferred_date}")
    if userdata.preferred_time:
        lines.append(f"Time: {userdata.preferred_time}")
    if userdata.spot_location:
        lines.append(f"Location: {userdata.spot_location}")
    if userdata.board_size:
        lines.append(f"Board: {userdata.board_size}")
    if userdata.wetsuit_size:
        lines.append(f"Wetsuit: {userdata.wetsuit_size}")
    if userdata.total_amount:
        lines.append(f"Total: ${userdata.total_amount:.2f}")
    
    return "\n".join(lines)


def format_gear_checklist() -> str:
    """Return a standard gear checklist for surf lessons.
    
    Returns:
        Checklist as formatted string
    """
    return """What to bring:
- Swimsuit (wear under wetsuit)
- Towel
- Sunscreen (reef-safe)
- Water bottle
- Change of clothes

We provide:
- Surfboard
- Wetsuit
- Leash
- Wax
- First aid kit"""

