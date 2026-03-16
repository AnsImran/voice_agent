from types import SimpleNamespace

import pytest

from tools.handoff_email_tools import (
    build_handoff_payload,
    get_smtp_config_from_env,
    send_handoff_email,
)


def test_get_smtp_config_from_env_missing_vars(monkeypatch):
    for key in [
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASS",
        "HANDOFF_FROM_EMAIL",
        "HANDOFF_TO_EMAIL",
    ]:
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValueError):
        get_smtp_config_from_env()


def test_build_handoff_payload_contains_canonical_sections():
    userdata = SimpleNamespace(
        name="Alex Smith",
        phone="949-555-1111",
        email="alex@example.com",
        dog_weight_lbs=42.0,
        dog_size="medium",
        requested_services=["daycare"],
        service_family="daycare",
        service_plan="golden_leash_club",
        selection_source="scheduler.book_slot",
        requested_date="2026-03-20",
        requested_time="09:00",
        booking_id="HH-1234ABCD",
        instructor_name="Brooke",
        quoted_subtotal=70.0,
        quoted_tax=0.0,
        quoted_total=70.0,
        quote_notes="Drop-in daycare",
        handoff_status="pending",
        payment_status="pending_human_followup",
        summarize=lambda: "summary text",
    )

    payload = build_handoff_payload(userdata)
    assert payload["customer"]["name"] == "Alex Smith"
    assert payload["dog_profile"]["size_tier"] == "medium"
    assert payload["request"]["services"] == ["daycare"]
    assert payload["request"]["service_family"] == "daycare"
    assert payload["request"]["service_plan"] == "golden_leash_club"
    assert payload["quote"]["total"] == 70.0
    assert payload["workflow"]["handoff_status"] == "pending"


class _FakeSMTPClient:
    def __init__(self):
        self.starttls_called = False
        self.login_called = False
        self.sent = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self):
        return None

    def starttls(self):
        self.starttls_called = True

    def login(self, user, password):
        self.login_called = bool(user and password)

    def send_message(self, msg):
        self.sent = msg["Subject"].startswith("Happy Hound Handoff -")


def _set_required_smtp_env(monkeypatch, port: str, use_tls: str | None):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", port)
    monkeypatch.setenv("SMTP_USER", "user@example.com")
    monkeypatch.setenv("SMTP_PASS", "secret")
    monkeypatch.setenv("HANDOFF_FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("HANDOFF_TO_EMAIL", "to@example.com")
    if use_tls is None:
        monkeypatch.delenv("SMTP_USE_TLS", raising=False)
    else:
        monkeypatch.setenv("SMTP_USE_TLS", use_tls)


def test_send_handoff_email_uses_smtp_ssl_on_port_465(monkeypatch):
    _set_required_smtp_env(monkeypatch, port="465", use_tls=None)
    ssl_client = _FakeSMTPClient()

    called = {"smtp": 0, "smtp_ssl": 0}

    def _smtp_ctor(*args, **kwargs):
        called["smtp"] += 1
        return _FakeSMTPClient()

    def _smtp_ssl_ctor(*args, **kwargs):
        called["smtp_ssl"] += 1
        return ssl_client

    monkeypatch.setattr("tools.handoff_email_tools.smtplib.SMTP", _smtp_ctor)
    monkeypatch.setattr("tools.handoff_email_tools.smtplib.SMTP_SSL", _smtp_ssl_ctor)

    send_handoff_email({"request": {"booking_id": "HH-TEST465"}})

    assert called["smtp"] == 0
    assert called["smtp_ssl"] == 1
    assert ssl_client.login_called is True
    assert ssl_client.sent is True
    assert ssl_client.starttls_called is False


def test_send_handoff_email_uses_starttls_on_non_465(monkeypatch):
    _set_required_smtp_env(monkeypatch, port="587", use_tls="true")
    smtp_client = _FakeSMTPClient()

    called = {"smtp": 0, "smtp_ssl": 0}

    def _smtp_ctor(*args, **kwargs):
        called["smtp"] += 1
        return smtp_client

    def _smtp_ssl_ctor(*args, **kwargs):
        called["smtp_ssl"] += 1
        return _FakeSMTPClient()

    monkeypatch.setattr("tools.handoff_email_tools.smtplib.SMTP", _smtp_ctor)
    monkeypatch.setattr("tools.handoff_email_tools.smtplib.SMTP_SSL", _smtp_ssl_ctor)

    send_handoff_email({"request": {"booking_id": "HH-TEST587"}})

    assert called["smtp"] == 1
    assert called["smtp_ssl"] == 0
    assert smtp_client.starttls_called is True
    assert smtp_client.login_called is True
    assert smtp_client.sent is True
