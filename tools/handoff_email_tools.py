"""SMTP handoff email tooling for structured Happy Hound session state."""
from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any


@dataclass
class SMTPConfig:
    host: str
    port: int
    user: str
    password: str
    use_tls: bool
    from_email: str
    to_email: str
    cc_email: str | None = None


def _parse_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_smtp_config_from_env() -> SMTPConfig:
    """Load and validate SMTP + routing settings from environment variables."""
    required_keys = [
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASS",
        "HANDOFF_FROM_EMAIL",
        "HANDOFF_TO_EMAIL",
    ]
    missing = [key for key in required_keys if not os.getenv(key)]
    if missing:
        raise ValueError(
            "Missing required SMTP env vars: " + ", ".join(sorted(missing))
        )

    return SMTPConfig(
        host=os.environ["SMTP_HOST"].strip(),
        port=int(os.environ["SMTP_PORT"].strip()),
        user=os.environ["SMTP_USER"].strip(),
        password=os.environ["SMTP_PASS"].strip(),
        use_tls=_parse_bool(os.getenv("SMTP_USE_TLS"), default=True),
        from_email=os.environ["HANDOFF_FROM_EMAIL"].strip(),
        to_email=os.environ["HANDOFF_TO_EMAIL"].strip(),
        cc_email=(os.getenv("HANDOFF_CC_EMAIL") or "").strip() or None,
    )


def build_handoff_payload(userdata) -> dict[str, Any]:
    """Build canonical structured session-state payload for human handoff."""
    return {
        "customer": {
            "name": userdata.name,
            "phone": userdata.phone,
            "email": userdata.email,
        },
        "dog_profile": {
            "weight_lbs": userdata.dog_weight_lbs,
            "size_tier": userdata.dog_size,
        },
        "request": {
            "services": list(userdata.requested_services or []),
            "service_family": getattr(userdata, "service_family", None),
            "service_plan": getattr(userdata, "service_plan", None),
            "selection_source": getattr(userdata, "selection_source", None),
            "date": userdata.requested_date,
            "time": userdata.requested_time,
            "booking_id": userdata.booking_id,
            "assigned_staff": userdata.instructor_name,
        },
        "quote": {
            "subtotal": userdata.quoted_subtotal,
            "tax": userdata.quoted_tax,
            "total": userdata.quoted_total,
            "notes": userdata.quote_notes,
        },
        "workflow": {
            "handoff_status": userdata.handoff_status,
            "payment_status": userdata.payment_status,
            "summary": userdata.summarize(),
        },
    }


def send_handoff_email(payload: dict[str, Any]) -> dict[str, str]:
    """Send structured handoff payload via SMTP."""
    cfg = get_smtp_config_from_env()

    booking_id = (
        payload.get("request", {}).get("booking_id")
        or payload.get("request", {}).get("time")
        or "new-request"
    )
    subject = f"Happy Hound Handoff - {booking_id}"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_email
    msg["To"] = cfg.to_email
    if cfg.cc_email:
        msg["Cc"] = cfg.cc_email
    msg["Message-ID"] = make_msgid()

    msg.set_content(
        "Structured handoff payload from the voice agent:\n\n"
        + json.dumps(payload, indent=2, ensure_ascii=False)
    )

    try:
        # Port 465 typically requires implicit SSL; other ports use SMTP and
        # optionally STARTTLS depending on SMTP_USE_TLS.
        if cfg.port == 465:
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=20) as smtp:
                smtp.ehlo()
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as smtp:
                smtp.ehlo()
                if cfg.use_tls:
                    smtp.starttls()
                    smtp.ehlo()
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(msg)
    except Exception as exc:
        raise RuntimeError(f"SMTP send failed: {exc}") from exc

    return {
        "message_id": msg["Message-ID"] or "",
        "subject": subject,
    }
