"""Unit tests for FileEmailClient: inbound parse + outbox reply/attachment (spec §7.3)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.email.file import FileEmailClient
from app.models import InboundEmail


def _inbound():
    return InboundEmail(
        message_id="msg-1",
        thread_id="thread-1",
        from_addr="user@example.com",
        to_addr="agent@agentmail.to",
        subject="Need exhibits",
        body_text="M12205 exhibits please",
        received_at=datetime(2026, 6, 28, tzinfo=timezone.utc),
    )


async def test_parse_inbound_canonical_fields():
    client = FileEmailClient()
    parsed = await client.parse_inbound(
        {
            "message_id": "msg-1",
            "thread_id": "thread-1",
            "from_addr": "user@example.com",
            "to_addr": "agent@agentmail.to",
            "subject": "hi",
            "body_text": "hello",
        }
    )
    assert parsed.message_id == "msg-1"
    assert parsed.from_addr == "user@example.com"
    assert parsed.received_at is not None  # defaulted to now


async def test_parse_inbound_aliases():
    client = FileEmailClient()
    parsed = await client.parse_inbound(
        {"message_id": "x", "from": "a@b.com", "to": "c@d.com", "text": "body"}
    )
    assert parsed.from_addr == "a@b.com"
    assert parsed.to_addr == "c@d.com"
    assert parsed.body_text == "body"


async def test_send_reply_writes_eml(tmp_path):
    client = FileEmailClient(outbox_dir=tmp_path)
    await client.send_reply(in_reply_to=_inbound(), body="Here are your docs.")
    files = list(tmp_path.glob("*.eml"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "To: user@example.com" in content
    assert "Subject: Re: Need exhibits" in content
    assert "In-Reply-To: msg-1" in content
    assert "Thread-Id: thread-1" in content
    assert "Here are your docs." in content


async def test_send_reply_copies_attachment(tmp_path):
    src = tmp_path / "docs.zip"
    src.write_bytes(b"PK\x03\x04 zip-bytes")
    client = FileEmailClient(outbox_dir=tmp_path / "out")
    await client.send_reply(
        in_reply_to=_inbound(), body="Attached.", attachment_path=str(src)
    )
    out = tmp_path / "out"
    eml = list(out.glob("*.eml"))[0]
    assert "X-Attachment:" in eml.read_text()
    copies = [p for p in out.glob("*docs.zip")]
    assert len(copies) == 1
    assert copies[0].read_bytes() == b"PK\x03\x04 zip-bytes"
