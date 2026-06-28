"""Local file-based email client (spec §7.3).

Used whenever ``ENV=local``. ``send_reply`` writes the reply body to an ``.eml``-style file under
``./outbox/`` and copies any attachment beside it, so the whole pipeline — thread-reply included —
is exercisable locally with no real email provider. ``parse_inbound`` accepts a permissive dict
(the local test harness builds these directly) and maps it onto ``InboundEmail``.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.email.base import EmailClient
from app.models import InboundEmail
from app.observability import get_logger

log = get_logger(__name__)


def _safe(token: str) -> str:
    """Make a string safe for use in a filename."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", token)[:80] or "unknown"


class FileEmailClient(EmailClient):
    """Writes replies to ``./outbox/`` instead of sending real email."""

    def __init__(self, outbox_dir: str | Path = "outbox") -> None:
        self.outbox = Path(outbox_dir)
        self.outbox.mkdir(parents=True, exist_ok=True)

    async def parse_inbound(self, raw: dict) -> InboundEmail:
        """Map a local/test inbound dict onto ``InboundEmail``.

        Accepts both the canonical ``InboundEmail`` field names and a few common aliases so test
        fixtures are easy to author.
        """
        received = raw.get("received_at")
        if received is None:
            received = datetime.now(timezone.utc)
        return InboundEmail(
            message_id=raw["message_id"],
            thread_id=raw.get("thread_id"),
            from_addr=raw.get("from_addr") or raw["from"],
            to_addr=raw.get("to_addr") or raw["to"],
            subject=raw.get("subject", ""),
            body_text=raw.get("body_text") or raw.get("text", ""),
            received_at=received,
        )

    async def send_reply(
        self,
        *,
        in_reply_to: InboundEmail,
        body: str,
        attachment_path: str | None = None,
    ) -> None:
        """Write the reply (and any attachment) to ``./outbox/``."""
        # Include the message id in the stem so a burst of replies to the same sender within the
        # same microsecond cannot collide and overwrite each other.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        stem = f"{ts}_{_safe(in_reply_to.message_id)}_{_safe(in_reply_to.from_addr)}"
        eml_path = self.outbox / f"{stem}.eml"

        attachment_note = ""
        if attachment_path:
            src = Path(attachment_path)
            dest = self.outbox / f"{stem}_{_safe(src.name)}"
            shutil.copyfile(src, dest)
            attachment_note = f"X-Attachment: {dest.name}\n"

        # A minimal .eml-style record: headers that preserve threading + the body.
        headers = (
            f"To: {in_reply_to.from_addr}\n"
            f"From: {in_reply_to.to_addr}\n"
            f"Subject: Re: {in_reply_to.subject}\n"
            f"In-Reply-To: {in_reply_to.message_id}\n"
            f"Thread-Id: {in_reply_to.thread_id or ''}\n"
            f"{attachment_note}"
            "\n"
        )
        eml_path.write_text(headers + body, encoding="utf-8")
        log.info(
            "file_email_written",
            path=str(eml_path),
            to=in_reply_to.from_addr,
            attachment=attachment_note.strip() or None,
        )
