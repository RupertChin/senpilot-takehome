"""Live LLM tests against the Anthropic API (spec §7.4). Marked ``llm`` — run with ``-m llm``.

Asserts on structure/invariants, not exact prose. Requires ANTHROPIC_API_KEY (read from .env).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.llm.anthropic_client import AnthropicLLM, summary_is_valid
from app.models import DocCounts, InboundEmail, MatterMetadata, ScrapeResult

pytestmark = pytest.mark.llm


@pytest.fixture
def llm():
    settings = Settings()  # reads .env for ANTHROPIC_API_KEY
    if not settings.anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    return AnthropicLLM(settings)


def _email(subject, body, message_id="m-llm"):
    return InboundEmail(
        message_id=message_id,
        thread_id="t-llm",
        from_addr="user@example.com",
        to_addr="agent@agentmail.to",
        subject=subject,
        body_text=body,
        received_at=datetime(2026, 6, 28, tzinfo=timezone.utc),
    )


async def test_classify_request(llm):
    label = await llm.classify(_email("docs", "Can you send me the Exhibits for matter M12205?"))
    assert label == "request"


async def test_classify_conversational(llm):
    label = await llm.classify(_email("thanks", "Thanks so much, that's all I needed!"))
    assert label == "conversational"


async def test_classify_junk(llm):
    label = await llm.classify(
        _email("WIN A PRIZE", "Congratulations!!! Click here to claim your free cruise now!!!")
    )
    assert label == "junk"


async def test_extract_matter_and_type(llm):
    parsed = await llm.extract(_email("docs", "Please pull the Exhibits for M12205."))
    assert parsed.matter_number == "M12205"
    assert parsed.document_type == "Exhibits"


async def test_extract_fuzzy_doc_type(llm):
    parsed = await llm.extract(_email("docs", "can I get the other docs for m12205"))
    assert parsed.matter_number == "M12205"
    assert parsed.document_type == "Other Documents"


async def test_extract_missing_matter_is_null(llm):
    parsed = await llm.extract(_email("docs", "Can you send me the transcripts please?"))
    assert parsed.matter_number is None
    assert parsed.document_type == "Transcripts"


async def test_extract_metadata_from_results_text(llm):
    text = (
        "M12205\n"
        "Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000\n"
        "Capital Expenditure Approvals\n"
        "Water\n"
        "Awaiting Compliance\n"
        "Date Received 04/07/2025\n"
        "Final Filing 10/23/2025\n"
        "Exhibits - 13   Key Documents - 5   Other Documents - 42\n"
    )
    md = await llm.extract_metadata("M12205", text)
    assert md.matter_number == "M12205"
    assert "Halifax Regional Water Commission" in (md.organization or "")
    assert md.amount == "$69,275,000"
    assert "Capital Expenditure" in (md.type or "")
    assert md.category == "Water"
    assert md.date_initial == "04/07/2025"
    assert md.date_final == "10/23/2025"


async def test_summarize_preserves_numbers(llm):
    md = MatterMetadata(
        matter_number="M12205",
        organization="Halifax Regional Water Commission",
        project="Windsor Street Exchange Redevelopment Project",
        amount="$69,275,000",
        type="Capital Expenditure Approvals",
        category="Water",
        status="Awaiting Compliance",
        date_initial="04/07/2025",
        date_final="10/23/2025",
        counts=DocCounts(exhibits=13, key_documents=5, other_documents=42),
    )
    scrape = ScrapeResult(
        matter_number="M12205",
        found=True,
        metadata=md,
        requested_type="Other Documents",
        type_count=42,
        documents=[],
        requested=10,
        downloaded=10,
        failed=0,
    )
    body = await llm.summarize(scrape, "attach", None)
    assert summary_is_valid(body, scrape)  # matter + "10 of (the) 10" preserved
    assert "M12205" in body
