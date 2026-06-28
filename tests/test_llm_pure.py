"""Offline tests for the LLM layer's deterministic logic (spec §5, §7.4).

Covers the §5 renderer, the summary validation gate, the coercion helpers, and the
summarize fallback orchestration (model-drift -> deterministic template) with a stubbed call —
no network.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.llm import prompts
from app.llm.anthropic_client import (
    AnthropicLLM,
    _coerce_doc_type,
    _coerce_matter,
    summary_is_valid,
)
from app.models import DocCounts, MatterMetadata, ScrapeResult


def _scrape(downloaded=10, requested=10, failed=0, doc_type="Other Documents"):
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
        counts=DocCounts(
            exhibits=13, key_documents=5, other_documents=42, transcripts=0, recordings=0
        ),
    )
    return ScrapeResult(
        matter_number="M12205",
        found=True,
        metadata=md,
        requested_type=doc_type,
        type_count=42,
        documents=[],
        requested=requested,
        downloaded=downloaded,
        failed=failed,
    )


# ── §5 renderer ───────────────────────────────────────────────────────────────


def test_render_attach_body_has_all_facts():
    body = prompts.render_success_body(_scrape(), "attach", None)
    assert "Hi, M12205 is about Halifax Regional Water Commission" in body
    assert "$69,275,000" in body
    assert "Capital Expenditure Approvals within the Water category" in body
    assert "initial filing on 04/07/2025 and a final filing on 10/23/2025" in body
    assert "13 Exhibits, 5 Key Documents, 42 Other Documents, 0 Transcripts, and 0 Recordings" in body
    assert "I downloaded 10 of the 42 Other Documents and have attached them as a ZIP." in body


def test_render_link_body_states_size_and_reason():
    body = prompts.render_success_body(
        _scrape(), "link", "https://x/jobs/abc.zip", link_size_mb=23.4, link_ttl_hours=72
    )
    assert "packaged them as a ZIP. It's 23MB — too large to attach" in body
    assert "https://x/jobs/abc.zip" in body
    assert "link expires in 72h)" in body


def test_render_partial_body_mentions_failed():
    body = prompts.render_success_body(_scrape(downloaded=7, requested=10, failed=3), "attach", None)
    assert "I downloaded 7 of the 42 Other Documents (3 could not be retrieved and were skipped)" in body


def test_render_tolerates_missing_metadata_fields():
    s = _scrape()
    s.metadata.organization = None
    s.metadata.amount = None
    body = prompts.render_success_body(s, "attach", None)
    assert "an undisclosed amount" in body
    assert "M12205" in body  # still renders


# ── summary validation gate ───────────────────────────────────────────────────


def _full_valid_body(s):
    """A model reply that preserves every authoritative fact for ``s``."""
    return prompts.render_success_body(s, "attach", None)


def test_summary_is_valid_accepts_full_correct_text():
    s = _scrape()
    assert summary_is_valid(_full_valid_body(s), s) is True


def test_summary_is_valid_rejects_missing_matter_or_kn():
    s = _scrape()
    body = _full_valid_body(s)
    assert summary_is_valid(body.replace("M12205", "MXXXX"), s) is False  # no matter
    assert summary_is_valid(body.replace("10 of the 42", "all of the"), s) is False  # no k-of-N
    assert summary_is_valid("", s) is False


def test_summary_is_valid_rejects_wrong_type_label_in_kn_sentence():
    # The k-of-N denominator is the type total (42), and the type label must be exact: a paraphrase
    # like "10 of the 42 other emails" must be rejected so it falls back to the deterministic body.
    s = _scrape()
    body = _full_valid_body(s)
    assert "10 of the 42 Other Documents" in body  # sanity: denominator is the type total
    drifted = body.replace("of the 42 Other Documents", "of the 42 other emails")
    assert summary_is_valid(drifted, s) is False


def test_summary_is_valid_rejects_invented_count():
    # Matter + k-of-N intact, but a per-type count was changed -> must fail (numbers authoritative).
    s = _scrape()
    body = _full_valid_body(s).replace("13 Exhibits", "99 Exhibits")
    assert summary_is_valid(body, s) is False


def test_summary_is_valid_requires_link_on_link_branch():
    s = _scrape()
    link = "https://x/jobs/abc.zip"
    body = prompts.render_success_body(s, "link", link, link_size_mb=23, link_ttl_hours=72)
    assert summary_is_valid(body, s, delivery="link", link=link) is True
    # A reply that dropped the link must not pass the gate.
    assert summary_is_valid(body.replace(link, "the link"), s, delivery="link", link=link) is False


# ── coercion helpers ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [("m12205", "M12205"), (" M 12205 ", "M12205"), ("M1234", "M1234"), (None, None),
     ("not-a-matter", None), ("M123", None), ("", None)],
)
def test_coerce_matter(raw, expected):
    assert _coerce_matter(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("Other Documents", "Other Documents"), ("other documents", "Other Documents"),
     ("Exhibits", "Exhibits"), ("nonsense", None), (None, None)],
)
def test_coerce_doc_type(raw, expected):
    assert _coerce_doc_type(raw) == expected


# ── summarize orchestration (stubbed call, no network) ────────────────────────


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


def _make(settings):
    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.settings = settings
    llm.client = None  # not used; _call is stubbed
    return llm


async def test_summarize_uses_model_text_when_valid(monkeypatch):
    llm = _make(Settings(_env_file=None))
    # A full, fact-preserving paragraph (matter + k-of-N + all five counts) with extra prose.
    good = prompts.render_success_body(_scrape(), "attach", None) + " Let me know if you need more."

    async def fake_call(**kwargs):
        return _Resp(good)

    monkeypatch.setattr(llm, "_call", fake_call)
    out = await llm.summarize(_scrape(), "attach", None)
    assert out == good


async def test_summarize_falls_back_to_template_on_drift(monkeypatch):
    llm = _make(Settings(_env_file=None))

    async def fake_call(**kwargs):
        return _Resp("Sure! Everything is done, have a nice day.")  # drops matter + numbers

    monkeypatch.setattr(llm, "_call", fake_call)
    out = await llm.summarize(_scrape(), "attach", None)
    # Fell back to the deterministic §5 template.
    assert out == prompts.render_success_body(_scrape(), "attach", None)
    assert "I downloaded 10 of the 42 Other Documents" in out


async def test_summarize_falls_back_when_model_invents_a_count(monkeypatch):
    llm = _make(Settings(_env_file=None))
    # Matter + k-of-N preserved, but a per-type count is wrong -> must fall back to the template.
    drifted = prompts.render_success_body(_scrape(), "attach", None).replace(
        "13 Exhibits", "99 Exhibits"
    )

    async def fake_call(**kwargs):
        return _Resp(drifted)

    monkeypatch.setattr(llm, "_call", fake_call)
    out = await llm.summarize(_scrape(), "attach", None)
    assert out == prompts.render_success_body(_scrape(), "attach", None)
    assert "13 Exhibits" in out  # the authoritative count, not the model's 99


async def test_summarize_falls_back_when_generation_raises(monkeypatch):
    llm = _make(Settings(_env_file=None))

    async def boom(**kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr(llm, "_call", boom)
    out = await llm.summarize(_scrape(), "link", "https://x/a.zip", link_size_mb=20, link_ttl_hours=48)
    assert "too large to attach" in out and "https://x/a.zip" in out
