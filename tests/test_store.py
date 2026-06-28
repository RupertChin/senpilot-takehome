"""Unit tests for InMemoryStore: idempotency claim, job persistence, thread context (spec §7.9)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import InboundEmail, JobRecord, ThreadContext
from app.store.memory_store import InMemoryStore


def _now():
    return datetime.now(timezone.utc)


def _inbound(message_id="m1", thread_id="t1"):
    return InboundEmail(
        message_id=message_id,
        thread_id=thread_id,
        from_addr="user@example.com",
        to_addr="agent@agentmail.to",
        subject="docs",
        body_text="M12205 exhibits",
        received_at=_now(),
    )


def _job(store_id="m1"):
    now = _now()
    return JobRecord(
        job_id=f"job-{store_id}",
        message_id=store_id,
        status="processing",
        inbound=_inbound(message_id=store_id),
        created_at=now,
        updated_at=now,
    )


async def test_claim_message_is_idempotent():
    store = InMemoryStore()
    assert await store.claim_message("m1") is True
    assert await store.claim_message("m1") is False  # already seen
    assert await store.claim_message("m2") is True


async def test_save_and_load_job():
    store = InMemoryStore()
    await store.claim_message("m1")
    job = _job("m1")
    await store.save_job(job)
    loaded = await store.load_job("m1")
    assert loaded is not None
    assert loaded.job_id == "job-m1"
    assert loaded.status == "processing"


async def test_load_unknown_job_returns_none():
    store = InMemoryStore()
    assert await store.load_job("nope") is None


async def test_set_status_updates_record():
    import asyncio

    store = InMemoryStore()
    await store.claim_message("m1")
    job = _job("m1")
    await store.save_job(job)
    before = (await store.load_job("m1")).updated_at
    await asyncio.sleep(0.001)  # ensure the clock advances
    await store.set_status("job-m1", "done")
    loaded = await store.load_job("m1")
    assert loaded.status == "done"
    assert loaded.updated_at > before  # timestamp actually advanced


async def test_set_status_unknown_job_is_noop():
    store = InMemoryStore()
    await store.set_status("ghost", "done")  # must not raise


async def test_thread_upsert_and_get():
    store = InMemoryStore()
    assert await store.get_thread("t1") is None
    ctx = ThreadContext(
        thread_id="t1",
        last_matter_number="M12205",
        last_document_type="Exhibits",
        updated_at=_now(),
    )
    await store.upsert_thread(ctx)
    got = await store.get_thread("t1")
    assert got is not None
    assert got.last_matter_number == "M12205"
    assert got.last_document_type == "Exhibits"

    # Upsert overwrites.
    ctx2 = ThreadContext(
        thread_id="t1",
        last_matter_number="M99999",
        last_document_type="Transcripts",
        updated_at=_now(),
    )
    await store.upsert_thread(ctx2)
    got2 = await store.get_thread("t1")
    assert got2.last_matter_number == "M99999"


async def test_concurrent_claims_single_winner():
    import asyncio

    store = InMemoryStore()
    results = await asyncio.gather(*[store.claim_message("m1") for _ in range(20)])
    assert sum(1 for r in results if r) == 1  # exactly one winner
