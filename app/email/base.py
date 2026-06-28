"""Email client interface (spec §7.3).

Two implementations back this ABC: ``AgentMailClient`` (prod, phase 8) and ``FileEmailClient``
(local — writes replies to ``./outbox/``). The pipeline depends only on this interface, so the
two are interchangeable by ``ENV``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import InboundEmail


class EmailClient(ABC):
    """Abstract email transport. Parses inbound payloads and sends in-thread replies."""

    @abstractmethod
    async def parse_inbound(self, raw: dict) -> InboundEmail:
        """Map a provider-specific inbound payload onto ``InboundEmail``."""
        ...

    @abstractmethod
    async def send_reply(
        self,
        *,
        in_reply_to: InboundEmail,
        body: str,
        attachment_path: str | None = None,
    ) -> None:
        """Send a reply in-thread (preserving threading), optionally with one attachment."""
        ...

    def verify_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """Verify an inbound webhook signature (spec §7.1).

        Default: no verification (the local ``FileEmailClient`` has no signing secret). The prod
        ``AgentMailClient`` overrides this to HMAC-verify the ``X-AgentMail-Signature`` header.
        """
        return True
