"""SMTP handoff email tooling for structured Happy Hound session state."""
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from datetime import datetime as _dt
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


# ---------------------------------------------------------------------------
# Email formatting helpers
# ---------------------------------------------------------------------------

def _fmt_date(iso_str: str | None) -> str:
    """'2026-03-31' or '2026-03-31T08:00' -> 'Tuesday, March 31, 2026'"""
    if not iso_str:
        return ""
    try:
        dt = _dt.fromisoformat(iso_str[:10])
        return dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")
    except Exception:
        return iso_str


def _fmt_time(iso_str: str | None) -> str:
    """'2026-03-31T08:00' -> '8:00 AM', '09:00' -> '9:00 AM'"""
    if not iso_str:
        return ""
    # Full ISO datetime (contains 'T')
    if "T" in iso_str:
        try:
            dt = _dt.fromisoformat(iso_str)
            h = dt.hour % 12 or 12
            return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"
        except Exception:
            return iso_str
    # Plain HH:MM (e.g. "09:00", "14:30")
    try:
        parts = iso_str.strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        h = hour % 12 or 12
        return f"{h}:{minute:02d} {'AM' if hour < 12 else 'PM'}"
    except (ValueError, IndexError):
        return iso_str


def _fmt_money(value: Any) -> str:
    """90.0 -> '$90.00'"""
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return str(value) if value is not None else "$0.00"


def _fmt_size(size_tier: str | None) -> str:
    return {
        "small": "Small",
        "medium": "Medium",
        "large": "Large",
        "x-large": "X-Large",
    }.get((size_tier or "").lower(), (size_tier or "").title())


def _fmt_service(payload: dict) -> str:
    """Derive a human-readable service name from the payload."""
    # The quote notes field usually starts with "{Service Name} at {time}..."
    notes = (payload.get("quote", {}).get("notes") or "").split("|")[0].strip()
    if " at " in notes:
        candidate = notes.split(" at ")[0].strip()
        if candidate:
            return candidate
    # Fallback: build from service_family + service_plan
    req = payload.get("request", {})
    family = (req.get("service_family") or "").replace("_", " ").title()
    plan = (req.get("service_plan") or "").replace("_", " ").title()
    return f"{plan} ({family})" if plan else family


def _clean_notes(notes: str | None, time_iso: str | None) -> list[str]:
    """Split notes on '|', strip boilerplate prefixes, replace ISO timestamps."""
    if not notes:
        return []
    result = []
    for part in notes.split("|"):
        part = part.strip()
        if not part:
            continue
        part = part.removeprefix("Additional caller note:").strip()
        if time_iso and time_iso in part:
            part = part.replace(time_iso, _fmt_time(time_iso))
        result.append(part)
    return result


def _build_plain_text(payload: dict) -> str:
    """Build a clean, human-readable plain-text email body."""
    cust = payload.get("customer", {})
    dog  = payload.get("dog_profile", {})
    req  = payload.get("request", {})
    quote = payload.get("quote", {})

    booking_id = req.get("booking_id") or "—"
    service    = _fmt_service(payload)
    date_str   = _fmt_date(req.get("date") or req.get("time"))
    time_str   = _fmt_time(req.get("time"))
    staff      = req.get("assigned_staff") or "To be assigned"

    weight = dog.get("weight_lbs")
    size   = _fmt_size(dog.get("size_tier"))
    weight_str = (f"{int(weight)} lbs ({size})" if weight else size) or "—"

    note_lines = _clean_notes(quote.get("notes"), req.get("time"))
    sep = "─" * 48

    lines: list[str] = [
        "HAPPY HOUND — NEW BOOKING REQUEST",
        f"Booking ID: {booking_id}",
        sep,
        "",
        "CUSTOMER",
        f"  Name:   {cust.get('name') or '—'}",
        f"  Phone:  {cust.get('phone') or '—'}",
    ]
    if cust.get("email"):
        lines.append(f"  Email:  {cust['email']}")

    lines += [
        "",
        "DOG",
        f"  Weight: {weight_str}",
        "",
        "APPOINTMENT",
        f"  Service: {service}",
        f"  Date:    {date_str}",
        f"  Time:    {time_str}",
        f"  Staff:   {staff}",
        "",
        "QUOTE",
        f"  Subtotal: {_fmt_money(quote.get('subtotal'))}",
        f"  Tax:      {_fmt_money(quote.get('tax'))}",
        f"  Total:    {_fmt_money(quote.get('total'))}",
    ]

    if note_lines:
        lines += ["", "NOTES"]
        for note in note_lines:
            lines.append(f"  {note}")

    lines += [
        "",
        sep,
        "Status: Pending staff confirmation",
        "Generated by Happy Hound Voice Agent",
    ]
    return "\n".join(lines)


def _build_html(payload: dict) -> str:
    """Build an HTML email body with the same information."""
    cust  = payload.get("customer", {})
    dog   = payload.get("dog_profile", {})
    req   = payload.get("request", {})
    quote = payload.get("quote", {})

    booking_id = req.get("booking_id") or "—"
    service    = _fmt_service(payload)
    date_str   = _fmt_date(req.get("date") or req.get("time"))
    time_str   = _fmt_time(req.get("time"))
    staff      = req.get("assigned_staff") or "To be assigned"

    weight = dog.get("weight_lbs")
    size   = _fmt_size(dog.get("size_tier"))
    weight_str = (f"{int(weight)} lbs ({size})" if weight else size) or "—"

    note_lines = _clean_notes(quote.get("notes"), req.get("time"))

    def row(label: str, value: str) -> str:
        return (
            f'<tr><td style="padding:6px 12px;color:#666;width:130px;'
            f'white-space:nowrap">{label}</td>'
            f'<td style="padding:6px 12px;color:#111">{value}</td></tr>'
        )

    def section(title: str, rows: list[str]) -> str:
        inner = "\n".join(rows)
        return (
            f'<tr><td colspan="2" style="padding:14px 12px 4px;font-weight:bold;'
            f'font-size:13px;color:#4a4a4a;text-transform:uppercase;'
            f'letter-spacing:.05em;border-top:1px solid #e8e8e8">{title}</td></tr>'
            f"\n{inner}"
        )

    customer_rows = [row("Name", cust.get("name") or "—"),
                     row("Phone", cust.get("phone") or "—")]
    if cust.get("email"):
        customer_rows.append(row("Email", cust["email"]))

    note_html = ""
    if note_lines:
        items = "".join(f"<li>{n}</li>" for n in note_lines)
        note_html = (
            f'<tr><td colspan="2" style="padding:14px 12px 4px;font-weight:bold;'
            f'font-size:13px;color:#4a4a4a;text-transform:uppercase;'
            f'letter-spacing:.05em;border-top:1px solid #e8e8e8">Notes</td></tr>'
            f'<tr><td colspan="2" style="padding:4px 12px 10px;color:#333">'
            f'<ul style="margin:0;padding-left:18px">{items}</ul></td></tr>'
        )

    table_rows = "\n".join([
        section("Customer", customer_rows),
        section("Dog", [row("Weight", weight_str)]),
        section("Appointment", [
            row("Service", service),
            row("Date", date_str),
            row("Time", time_str),
            row("Staff", staff),
        ]),
        section("Quote", [
            row("Subtotal", _fmt_money(quote.get("subtotal"))),
            row("Tax", _fmt_money(quote.get("tax"))),
            row("<strong>Total</strong>", f"<strong>{_fmt_money(quote.get('total'))}</strong>"),
        ]),
    ]) + note_html

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:24px 0">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:6px;overflow:hidden;
                    box-shadow:0 1px 4px rgba(0,0,0,.1)">
        <!-- header -->
        <tr>
          <td colspan="2" style="background:#1a3c5e;padding:20px 24px">
            <div style="color:#fff;font-size:18px;font-weight:bold">
              Happy Hound — New Booking Request
            </div>
            <div style="color:#a8c0d6;font-size:13px;margin-top:4px">
              Booking ID: {booking_id}
            </div>
          </td>
        </tr>
        <!-- body rows -->
        <table width="100%" cellpadding="0" cellspacing="0"
               style="font-size:14px;line-height:1.5">
          {table_rows}
        </table>
        <!-- footer -->
        <tr>
          <td colspan="2"
              style="padding:14px 24px;background:#f9f9f9;border-top:1px solid #e8e8e8;
                     font-size:12px;color:#888">
            Status: Pending staff confirmation &nbsp;·&nbsp;
            Generated by Happy Hound Voice Agent
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_subject(payload: dict) -> str:
    """Build a descriptive email subject line."""
    cust = payload.get("customer", {})
    req  = payload.get("request", {})
    booking_id = req.get("booking_id") or "new-request"
    name    = cust.get("name") or "Guest"
    service = _fmt_service(payload)
    date    = req.get("date") or req.get("time") or ""
    date_short = ""
    if date:
        try:
            dt = _dt.fromisoformat(date[:10])
            date_short = dt.strftime("%b ") + str(dt.day)
        except Exception:
            date_short = date[:10]
    parts = [p for p in [name, service, date_short] if p]
    return f"Happy Hound Booking — {' | '.join(parts)} [{booking_id}]"


# ---------------------------------------------------------------------------
# Public send function
# ---------------------------------------------------------------------------

def send_handoff_email(payload: dict[str, Any]) -> dict[str, str]:
    """Send a formatted handoff email via SMTP."""
    cfg = get_smtp_config_from_env()

    subject = _build_subject(payload)
    plain   = _build_plain_text(payload)
    html    = _build_html(payload)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = cfg.from_email
    msg["To"]      = cfg.to_email
    if cfg.cc_email:
        msg["Cc"] = cfg.cc_email
    msg["Message-ID"] = make_msgid()

    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    try:
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
