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
from collections.abc import Mapping
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


def svix_verify(secret: str, headers: Mapping[str, str], raw_body: bytes) -> bool:
    """Verify an AgentMail webhook signature using the Svix scheme. Pure + fail-closed.

    AgentMail signs webhooks with Svix (the SDK ships ``SvixId``/``SvixSignature``/``SvixTimestamp``
    types — that's the source of truth, not the simplified ``X-AgentMail-Signature`` hex-HMAC in the
    written docs, which never matched live traffic). The scheme:

      signed = f"{svix-id}.{svix-timestamp}.{raw_body}"
      expected = base64( HMAC_SHA256( base64decode(secret without 'whsec_'), signed ) )

    The ``svix-signature`` header is a space-separated list of ``v1,<sig>`` entries; any match passes.
    Header names are checked under both the ``svix-`` and the unbranded ``webhook-`` prefixes.
    """
    g = headers.get
    msg_id = g("svix-id") or g("webhook-id")
    timestamp = g("svix-timestamp") or g("webhook-timestamp")
    sig_header = g("svix-signature") or g("webhook-signature")
    if not (msg_id and timestamp and sig_header):
        return False
    key_b64 = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        key = base64.b64decode(key_b64)
    except Exception:  # noqa: BLE001 — a malformed secret is a config error → fail closed
        return False
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in sig_header.split():
        candidate = part.split(",", 1)[1] if "," in part else part
        if hmac.compare_digest(candidate, expected):
            return True
    return False


class AgentMailClient(EmailClient):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.inbox = settings.agentmail_inbox
        self.webhook_secret = settings.agentmail_webhook_secret
        self.client = AsyncAgentMail(api_key=settings.agentmail_api_key)

    # ── signature verification (spec §7.1) ────────────────────────────────────
    def verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        """Verify the inbound webhook's Svix signature from its headers. Fail closed on anything odd."""
        if not self.webhook_secret:
            log.warning("agentmail_webhook_secret_unset")
            return False  # fail closed — a prod inbox must configure the secret
        return svix_verify(self.webhook_secret, headers, raw_body)

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
        attachment_url: str | None = None,
        attachment_filename: str | None = None,
    ) -> None:
        attachments = None
        if attachment_path:
            # Inline base64 — only for small files (the request body is gateway-capped; large base64
            # bodies are rejected 413, which is why bigger zips use the url path below instead).
            data = Path(attachment_path).read_bytes()
            attachments = [
                SendAttachment(
                    filename=Path(attachment_path).name,
                    content_type="application/zip",
                    content=base64.b64encode(data).decode(),
                    content_disposition="attachment",
                )
            ]
        elif attachment_url:
            # AgentMail fetches the URL server-side and attaches the real file — no base64 in our
            # request, so this carries large zips (up to the email size limit) without a 413.
            attachments = [
                SendAttachment(
                    filename=attachment_filename or "documents.zip",
                    content_type="application/zip",
                    url=attachment_url,
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
            attachment="inline" if attachment_path else ("url" if attachment_url else None),
        )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
