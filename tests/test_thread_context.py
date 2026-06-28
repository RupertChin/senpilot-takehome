"""Offline tests for the thread-context follow-up feature (spec §7.9) — the differentiator.

inherit-on-null, explicit-overrides-inherited, upsert-after-successful-scrape, all via the
in-memory store (no database).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.email.file import FileEmailClient
from app.models import (
    DownloadedDoc,
    InboundEmail,
    JobRecord,
    MatterMetadata,
    ParsedRequest,
    ScrapeResult,
    ThreadContext,
)
from app.pipeline import PipelineDeps, process_job
from app.store.memory_store import InMemoryStore


def _inbound(message_id="m-1", thread_id="t-1"):
    return InboundEmail(
        message_id=message_id,
        thread_id=thread_id,
        from_addr="user@example.com",
        to_addr="agent@agentmail.to",
        subject="docs",
        body_text="follow-up",
        received_at=datetime(2026, 6, 28, tzinfo=timezone.utc),
    )


def _metadata():
    return MatterMetadata(matter_number="M12205")


class FakeLLM:
    def __init__(self, parsed):
        self._parsed = parsed

    async def classify(self, email):
        return "request"

    async def extract(self, email):
        return self._parsed

    async def extract_metadata(self, matter, text):
        return _metadata()

    async def summarize(self, *a, **k):
        return "ok"


def _capturing_scrape(captured):
    async def fake_scrape(matter, doc_type, *, settings, extract_metadata):
        captured["matter"] = matter
        captured["doc_type"] = doc_type
        # a one-doc successful scrape
        p = settings  # unused
        return ScrapeResult(
            matter_number=matter,
            found=True,
            metadata=_metadata(),
            requested_type=doc_type,
            type_count=42,
            documents=[DownloadedDoc(doc_no="d", filenames=["d.pdf"], paths=[], total_bytes=0)],
            requested=1,
            downloaded=1,
            failed=0,
        )

    return fake_scrape


async def _run(deps, inbound, message_id="m-1"):
    now = datetime.now(timezone.utc)
    await deps.store.claim_message(inbound.message_id)
    await deps.store.save_job(
        JobRecord(
            job_id=f"job-{message_id}",
            message_id=inbound.message_id,
            status="processing",
            inbound=inbound,
            created_at=now,
            updated_at=now,
        )
    )
    await process_job(inbound.message_id, deps=deps)


def _deps(tmp_path, parsed, store=None):
    return PipelineDeps(
        store=store or InMemoryStore(),
        email=FileEmailClient(outbox_dir=tmp_path),
        llm=FakeLLM(parsed),
        settings=Settings(_env_file=None),
    )


# ── inherit-on-null ───────────────────────────────────────────────────────────


async def test_inherits_matter_when_null(tmp_path, monkeypatch):
    store = InMemoryStore()
    await store.upsert_thread(
        ThreadContext(thread_id="t-1", last_matter_number="M12205", last_document_type="Exhibits",
                      updated_at=datetime.now(timezone.utc))
    )
    captured = {}
    monkeypatch.setattr("app.pipeline.scrape_matter", _capturing_scrape(captured))
    # New email gives an explicit type but no matter -> inherit matter from the thread.
    deps = _deps(tmp_path, ParsedRequest(matter_number=None, document_type="Transcripts"), store)
    await _run(deps, _inbound())
    assert captured["matter"] == "M12205"  # inherited
    assert captured["doc_type"] == "Transcripts"  # explicit, not the stored Exhibits


async def test_inherits_type_when_null(tmp_path, monkeypatch):
    store = InMemoryStore()
    await store.upsert_thread(
        ThreadContext(thread_id="t-1", last_matter_number="M12205", last_document_type="Exhibits",
                      updated_at=datetime.now(timezone.utc))
    )
    captured = {}
    monkeypatch.setattr("app.pipeline.scrape_matter", _capturing_scrape(captured))
    deps = _deps(tmp_path, ParsedRequest(matter_number="M99999", document_type=None), store)
    await _run(deps, _inbound())
    assert captured["matter"] == "M99999"  # explicit overrides stored matter
    assert captured["doc_type"] == "Exhibits"  # inherited type


# ── explicit overrides inherited ──────────────────────────────────────────────


async def test_explicit_overrides_inherited(tmp_path, monkeypatch):
    store = InMemoryStore()
    await store.upsert_thread(
        ThreadContext(thread_id="t-1", last_matter_number="M12205", last_document_type="Exhibits",
                      updated_at=datetime.now(timezone.utc))
    )
    captured = {}
    monkeypatch.setattr("app.pipeline.scrape_matter", _capturing_scrape(captured))
    deps = _deps(tmp_path, ParsedRequest(matter_number="M99999", document_type="Recordings"), store)
    await _run(deps, _inbound())
    assert captured["matter"] == "M99999"
    assert captured["doc_type"] == "Recordings"


# ── no thread context -> clarification (no inherit) ───────────────────────────


async def test_no_context_null_field_clarifies(tmp_path, monkeypatch):
    # Empty thread store + a null matter -> clarification, scrape never called.
    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("scrape should not run")

    monkeypatch.setattr("app.pipeline.scrape_matter", boom)
    deps = _deps(tmp_path, ParsedRequest(matter_number=None, document_type="Exhibits"))
    await _run(deps, _inbound())
    body = list(tmp_path.glob("*.eml"))[0].read_text()
    assert "I need a matter number" in body
    assert called["n"] == 0


# ── upsert after a successful scrape ──────────────────────────────────────────


async def test_upsert_after_successful_scrape(tmp_path, monkeypatch):
    store = InMemoryStore()
    captured = {}
    monkeypatch.setattr("app.pipeline.scrape_matter", _capturing_scrape(captured))
    deps = _deps(tmp_path, ParsedRequest(matter_number="M12205", document_type="Other Documents"), store)
    await _run(deps, _inbound())
    ctx = await store.get_thread("t-1")
    assert ctx is not None
    assert ctx.last_matter_number == "M12205"
    assert ctx.last_document_type == "Other Documents"


async def test_no_thread_id_skips_context(tmp_path, monkeypatch):
    # An email with no thread_id neither inherits nor upserts (no crash).
    store = InMemoryStore()
    captured = {}
    monkeypatch.setattr("app.pipeline.scrape_matter", _capturing_scrape(captured))
    deps = _deps(tmp_path, ParsedRequest(matter_number="M12205", document_type="Exhibits"), store)
    inbound = _inbound()
    inbound.thread_id = None
    await _run(deps, inbound)
    assert captured["matter"] == "M12205"  # ran fine


async def test_empty_type_still_remembers_matter(tmp_path, monkeypatch):
    # An empty-type result (found, 0 of that type) still upserts the matter for follow-ups.
    store = InMemoryStore()

    async def empty_scrape(matter, doc_type, *, settings, extract_metadata):
        return ScrapeResult(
            matter_number=matter, found=True, metadata=_metadata(),
            requested_type=doc_type, type_count=0,
        )

    monkeypatch.setattr("app.pipeline.scrape_matter", empty_scrape)
    deps = _deps(tmp_path, ParsedRequest(matter_number="M12205", document_type="Transcripts"), store)
    await _run(deps, _inbound())
    ctx = await store.get_thread("t-1")
    assert ctx is not None and ctx.last_matter_number == "M12205"


async def test_two_turn_inherit_through_process_job(tmp_path, monkeypatch):
    # End-to-end: turn 1 stores via the real pipeline; turn 2 (no matter) inherits it.
    store = InMemoryStore()
    captured = {}
    monkeypatch.setattr("app.pipeline.scrape_matter", _capturing_scrape(captured))

    # Turn 1: explicit matter + type -> stored.
    deps1 = _deps(tmp_path, ParsedRequest(matter_number="M12205", document_type="Other Documents"), store)
    await _run(deps1, _inbound(message_id="m-1", thread_id="thread-X"), message_id="m-1")
    assert captured["matter"] == "M12205"

    # Turn 2: same thread, only an explicit type, NO matter -> inherits M12205 from turn 1's upsert.
    deps2 = _deps(tmp_path, ParsedRequest(matter_number=None, document_type="Exhibits"), store)
    await _run(deps2, _inbound(message_id="m-2", thread_id="thread-X"), message_id="m-2")
    assert captured["matter"] == "M12205"  # inherited end-to-end
    assert captured["doc_type"] == "Exhibits"  # explicit on turn 2
    # Thread now reflects turn 2.
    ctx = await store.get_thread("thread-X")
    assert ctx.last_document_type == "Exhibits"
