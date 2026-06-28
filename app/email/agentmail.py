"""AgentMail email client (spec §7.3) — the prod implementation.

Bound against AgentMail's live SDK/docs (training priors are unreliable for it):
  - inbound: the ``message.received`` webhook payload's ``message`` block → ``InboundEmail``.
  - outbound: ``inboxes.messages.reply(inbox_id, message_id, …)`` replies IN-THREAD (preserving
    threading) with an optional base64 ``SendAttachment``.
  - signature: each inbound POST carries ``X-AgentMail-Signature`` = HMAC-SHA256(raw_body, secret);
    ``verify_signature`` recomputes it (the one case ``/inbound`` answers with 401, not 200).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from email.utils import parseaddr
from pathlib import Path

from agentmail import AsyncAgentMail
from agentmail.attachments.types.send_attachment import SendAttachment

from app.config import Settings
from app.email.base import EmailClient
from app.models import InboundEmail
from app.observability import get_logger

log = get_logger(__name__)


def _addr(value) -> str:
    """Extract a bare email from a 'Name <email>' string (or a list's first element)."""
    if isinstance(value, list):
        value = value[0] if value else ""
    return parseaddr(value or "")[1] or (value or "")


class AgentMailClient(EmailClient):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.inbox = settings.agentmail_inbox
        self.webhook_secret = settings.agentmail_webhook_secret
        self.client = AsyncAgentMail(api_key=settings.agentmail_api_key)

    # ── signature verification (spec §7.1) ────────────────────────────────────
    def verify_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """HMAC-SHA256(raw_body, secret) == X-AgentMail-Signature. Fail closed on anything odd."""
        if not self.webhook_secret:
            log.warning("agentmail_webhook_secret_unset")
            return False  # fail closed — a prod inbox must configure the secret
        if not isinstance(signature, str) or not signature:
            return False
        expected = hmac.new(
            self.webhook_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    # ── inbound parse (spec §7.3) ─────────────────────────────────────────────
    async def parse_inbound(self, raw: dict) -> InboundEmail:
        # The webhook envelope wraps the email under "message"; tolerate a bare message dict too.
        msg = raw.get("message", raw)
        to_addr = _addr(msg.get("to")) or self.inbox
        return InboundEmail(
            message_id=msg["message_id"],
            thread_id=msg.get("thread_id"),
            from_addr=_addr(msg.get("from")),
            to_addr=to_addr,
            subject=msg.get("subject") or "",
            body_text=msg.get("text") or "",
            received_at=msg.get("created_at") or _now(),
        )

    # ── outbound reply (spec §7.3) ────────────────────────────────────────────
    async def send_reply(
        self,
        *,
        in_reply_to: InboundEmail,
        body: str,
        attachment_path: str | None = None,
    ) -> None:
        attachments = None
        if attachment_path:
            data = Path(attachment_path).read_bytes()
            attachments = [
                SendAttachment(
                    filename=Path(attachment_path).name,
                    content_type="application/zip",
                    content=base64.b64encode(data).decode(),
                    content_disposition="attachment",
                )
            ]
        try:
            await self.client.inboxes.messages.reply(
                inbox_id=self.inbox,
                message_id=in_reply_to.message_id,
                text=body,
                attachments=attachments,
            )
        except Exception:
            # Secondary net (§7.7): if a synchronous oversize rejection slips past the threshold
            # gate, the pipeline already chose the link branch — re-raise so the failure surfaces.
            log.error("agentmail_send_failed", message_id=in_reply_to.message_id, exc_info=True)
            raise
        log.info(
            "agentmail_reply_sent",
            message_id=in_reply_to.message_id,
            attachment=bool(attachment_path),
        )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
