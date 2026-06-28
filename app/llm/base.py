"""LLM interface (spec §7.4).

``AnthropicLLM`` (phase/Stage 5) implements this. Keeping the pipeline behind the ABC makes the
model layer swappable. Numbers passed to ``summarize`` are authoritative facts and must be
preserved verbatim by the implementation (it asserts and falls back to a template otherwise).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from app.models import InboundEmail, MatterMetadata, ParsedRequest, ScrapeResult

Classification = Literal["request", "conversational", "junk"]


class LLM(ABC):
    """Abstract LLM: classify intent, extract a request, and compose the success summary."""

    @abstractmethod
    async def classify(self, email: InboundEmail) -> Classification:
        """Cheap, high-recall intent label: request | conversational | junk."""
        ...

    @abstractmethod
    async def extract(self, email: InboundEmail) -> ParsedRequest:
        """Structured extraction of ``{matter_number, document_type}`` (either may be null)."""
        ...

    @abstractmethod
    async def extract_metadata(self, matter_number: str, results_text: str) -> MatterMetadata:
        """Parse the live results-screen text into ``MatterMetadata`` (numbers read, not invented)."""
        ...

    @abstractmethod
    async def summarize(
        self,
        scrape: ScrapeResult,
        delivery: Literal["attach", "link"],
        link: str | None,
        *,
        link_size_mb: float | None = None,
        link_ttl_hours: int | None = None,
    ) -> str:
        """Compose the §5 success/partial body. Exact numbers from ``scrape`` must be preserved.

        ``link_size_mb``/``link_ttl_hours`` supply the §5 link-branch wording ("It's {size}MB …
        expires in {ttl}h"); they're optional kwargs because the bare (scrape, delivery, link)
        signature can't carry them. A minor interface refinement, not a §0/§6 change.
        """
        ...
