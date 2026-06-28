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


# ── signature verification (Svix scheme — AgentMail's real webhook signing) ─────

# A valid base64 secret with the whsec_ prefix, as AgentMail/Svix issue them.
_SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


def _svix_headers(secret: str, body: bytes, msg_id: str = "msg_1", ts: str = "1700000000") -> dict:
    """Build the Svix headers (svix-id/timestamp/signature) for a body, like AgentMail sends."""
    key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    signed = msg_id.encode() + b"." + ts.encode() + b"." + body
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}


def test_verify_signature_accepts_valid_svix():
    client = AgentMailClient(_settings(secret=_SECRET))
    body = b'{"event_type":"message.received"}'
    assert client.verify_signature(body, _svix_headers(_SECRET, body)) is True


def test_verify_signature_accepts_one_of_multiple_signatures():
    # The svix-signature header is a space-separated list; any valid v1 entry passes.
    client = AgentMailClient(_settings(secret=_SECRET))
    body = b'{"event_type":"message.received"}'
    good = _svix_headers(_SECRET, body)
    good["svix-signature"] = "v1,not-the-one " + good["svix-signature"]
    assert client.verify_signature(body, good) is True


def test_verify_signature_rejects_bad_tampered_and_missing():
    client = AgentMailClient(_settings(secret=_SECRET))
    body = b'{"event_type":"message.received"}'
    good = _svix_headers(_SECRET, body)
    assert client.verify_signature(body, {}) is False  # no signature headers at all
    bad = {**good, "svix-signature": "v1,deadbeef"}
    assert client.verify_signature(body, bad) is False  # wrong signature
    assert client.verify_signature(b'{"event_type":"y"}', good) is False  # tampered body
    missing_ts = {k: v for k, v in good.items() if k != "svix-timestamp"}
    assert client.verify_signature(body, missing_ts) is False  # incomplete headers


def test_verify_signature_fails_closed_without_secret():
    client = AgentMailClient(_settings(secret=""))
    body = b"x"
    assert client.verify_signature(body, _svix_headers(_SECRET, body)) is False


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


async def test_send_reply_attaches_by_url(monkeypatch):
    # The url-attach path: no base64 content, just a url the provider fetches server-side.
    client = AgentMailClient(_settings())
    captured = {}

    async def fake_reply(*, inbox_id, message_id, text, attachments):
        captured["attachments"] = attachments

    monkeypatch.setattr(client.client.inboxes.messages, "reply", fake_reply)
    inbound = InboundEmail(
        message_id="msg_2", thread_id="thd_2", from_addr="jane@example.com",
        to_addr="agent@agentmail.to", subject="docs", body_text="hi",
        received_at=datetime(2026, 6, 28, tzinfo=timezone.utc),
    )
    await client.send_reply(
        in_reply_to=inbound,
        body="Here you go.",
        attachment_url="https://storage.googleapis.com/bucket/jobs/abc.zip?sig=x",
        attachment_filename="M12205_Other_Documents.zip",
    )
    att = captured["attachments"][0]
    assert att.url == "https://storage.googleapis.com/bucket/jobs/abc.zip?sig=x"
    assert att.filename == "M12205_Other_Documents.zip"
    assert att.content_type == "application/zip"
    assert att.content is None  # no inline base64 — the whole point of the url path


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
    headers = _svix_headers(settings.agentmail_webhook_secret, body)
    assert client.verify_signature(body, headers) is True
    assert client.verify_signature(body, {**headers, "svix-signature": "v1,bad"}) is False
