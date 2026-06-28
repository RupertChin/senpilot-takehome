"""Tests for AgentMailClient (spec §7.3 / §7.1).

Offline: inbound webhook parse, HMAC signature verification, and the reply-with-attachment call
shape (mocked client). Live (marked `agentmail`): a real send + reply round-trip to the agent's
own inbox, plus signature verify with the real secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.email.agentmail import AgentMailClient
from app.models import InboundEmail


def _settings(secret="testsecret", inbox="agent@agentmail.to", key="am_test"):
    return Settings(
        agentmail_api_key=key,
        agentmail_inbox=inbox,
        agentmail_webhook_secret=secret,
        _env_file=None,
    )


def _webhook_payload(message_id="msg_1", thread_id="thd_1"):
    return {
        "type": "event",
        "event_type": "message.received",
        "event_id": "evt_1",
        "message": {
            "inbox_id": "inbox_1",
            "thread_id": thread_id,
            "message_id": message_id,
            "from": "Jane Doe <jane@example.com>",
            "to": ["Agent <agent@agentmail.to>"],
            "subject": "Need exhibits",
            "text": "Please send the Exhibits for M12205.",
            "html": "<p>...</p>",
            "created_at": "2026-06-28T10:00:00Z",
        },
    }


# ── inbound parse ─────────────────────────────────────────────────────────────


async def test_parse_inbound_maps_webhook_payload():
    client = AgentMailClient(_settings())
    email = await client.parse_inbound(_webhook_payload())
    assert email.message_id == "msg_1"
    assert email.thread_id == "thd_1"
    assert email.from_addr == "jane@example.com"  # extracted from "Name <email>"
    assert email.to_addr == "agent@agentmail.to"
    assert email.subject == "Need exhibits"
    assert email.body_text == "Please send the Exhibits for M12205."
    assert email.received_at.year == 2026


async def test_parse_inbound_tolerates_bare_message():
    client = AgentMailClient(_settings())
    msg = _webhook_payload()["message"]
    email = await client.parse_inbound(msg)  # not wrapped in {"message": ...}
    assert email.message_id == "msg_1"


# ── signature verification ────────────────────────────────────────────────────


def test_verify_signature_accepts_valid_hmac():
    client = AgentMailClient(_settings(secret="s3cr3t"))
    body = b'{"event":"x"}'
    sig = hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    assert client.verify_signature(body, sig) is True


def test_verify_signature_rejects_bad_and_missing():
    client = AgentMailClient(_settings(secret="s3cr3t"))
    body = b'{"event":"x"}'
    assert client.verify_signature(body, "deadbeef") is False
    assert client.verify_signature(body, None) is False
    assert client.verify_signature(body, "") is False
    # tamper with the body -> signature no longer matches
    sig = hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    assert client.verify_signature(b'{"event":"y"}', sig) is False


def test_verify_signature_fails_closed_without_secret():
    client = AgentMailClient(_settings(secret=""))
    body = b"x"
    assert client.verify_signature(body, "anything") is False


# ── reply with attachment (mocked client) ─────────────────────────────────────


async def test_send_reply_builds_attachment(monkeypatch, tmp_path):
    client = AgentMailClient(_settings())
    zip_path = tmp_path / "docs.zip"
    zip_path.write_bytes(b"PK\x03\x04 zip-bytes")

    captured = {}

    async def fake_reply(*, inbox_id, message_id, text, attachments):
        captured.update(
            inbox_id=inbox_id, message_id=message_id, text=text, attachments=attachments
        )

    monkeypatch.setattr(client.client.inboxes.messages, "reply", fake_reply)

    inbound = InboundEmail(
        message_id="msg_1", thread_id="thd_1", from_addr="jane@example.com",
        to_addr="agent@agentmail.to", subject="docs", body_text="hi",
        received_at=datetime(2026, 6, 28, tzinfo=timezone.utc),
    )
    await client.send_reply(in_reply_to=inbound, body="Here you go.", attachment_path=str(zip_path))

    assert captured["inbox_id"] == "agent@agentmail.to"
    assert captured["message_id"] == "msg_1"  # in-thread reply to the inbound message
    assert captured["text"] == "Here you go."
    att = captured["attachments"][0]
    assert att.filename == "docs.zip"
    assert att.content_type == "application/zip"
    assert base64.b64decode(att.content) == b"PK\x03\x04 zip-bytes"


async def test_send_reply_no_attachment(monkeypatch):
    client = AgentMailClient(_settings())
    captured = {}

    async def fake_reply(*, inbox_id, message_id, text, attachments):
        captured["attachments"] = attachments

    monkeypatch.setattr(client.client.inboxes.messages, "reply", fake_reply)
    inbound = InboundEmail(
        message_id="m", thread_id=None, from_addr="a@b.com", to_addr="agent@agentmail.to",
        subject="s", body_text="b", received_at=datetime(2026, 6, 28, tzinfo=timezone.utc),
    )
    await client.send_reply(in_reply_to=inbound, body="no attachment")
    assert captured["attachments"] is None


# ── live round-trip ───────────────────────────────────────────────────────────


@pytest.mark.agentmail
async def test_live_send_and_reply_roundtrip(tmp_path):
    settings = Settings()  # reads .env
    if not settings.agentmail_api_key or not settings.agentmail_inbox:
        pytest.skip("AGENTMAIL_API_KEY/AGENTMAIL_INBOX not set")
    client = AgentMailClient(settings)
    inbox = settings.agentmail_inbox

    # Seed a message to the agent's own inbox (self-contained, no external recipient).
    seed = await client.client.inboxes.messages.send(
        inbox_id=inbox, to=inbox, subject="senpilot self-test",
        text="seed", html="<p>seed</p>",
    )
    seed_id = getattr(seed, "message_id", None) or getattr(seed, "id", None)
    assert seed_id, f"no message id on send response: {seed!r}"

    # Reply in-thread with a ZIP attachment via the real send_reply path.
    zip_path = tmp_path / "senpilot-test.zip"
    zip_path.write_bytes(b"PK\x03\x04 senpilot live attachment test")
    inbound = InboundEmail(
        message_id=seed_id, thread_id=None, from_addr=inbox, to_addr=inbox,
        subject="senpilot self-test", body_text="seed",
        received_at=datetime.now(timezone.utc),
    )
    await client.send_reply(
        in_reply_to=inbound, body="Live attachment round-trip OK.", attachment_path=str(zip_path)
    )


@pytest.mark.agentmail
def test_live_signature_with_real_secret():
    settings = Settings()
    if not settings.agentmail_webhook_secret:
        pytest.skip("AGENTMAIL_WEBHOOK_SECRET not set")
    client = AgentMailClient(settings)
    body = b'{"event_type":"message.received"}'
    sig = hmac.new(settings.agentmail_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert client.verify_signature(body, sig) is True
    assert client.verify_signature(body, "bad") is False
