"""Live validation of SupabaseStore against the real project (spec §7.9). Marked `supabase`.

Runs the Store contract — idempotency claim, job save/load round-trip, status update, thread
upsert/get/overwrite — against live Supabase. Uses unique ids per run and cleans up after.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.models import InboundEmail, JobRecord, ThreadContext
from app.store.supabase_store import SupabaseStore

pytestmark = pytest.mark.supabase


@pytest.fixture
def store():
    settings = Settings()  # reads .env
    if not settings.supabase_url or not settings.supabase_key:
        pytest.skip("SUPABASE_URL/SUPABASE_KEY not set")
    return SupabaseStore(settings)


def _inbound(message_id):
    return InboundEmail(
        message_id=message_id,
        thread_id="t-sup",
        from_addr="user@example.com",
        to_addr="agent@agentmail.to",
        subject="docs",
        body_text="M12205 Other Documents",
        received_at=datetime(2026, 6, 28, tzinfo=timezone.utc),
    )


async def test_supabase_store_contract(store):
    mid = f"sup-{uuid.uuid4()}"
    job_id = str(uuid.uuid4())
    tid = f"thr-{uuid.uuid4()}"
    client = await store._client_()
    try:
        # ── idempotency claim ──────────────────────────────────────────────
        assert await store.claim_message(mid) is True  # newly claimed
        assert await store.claim_message(mid) is False  # already seen (UNIQUE gate)

        # ── save + load round-trip ─────────────────────────────────────────
        now = datetime.now(timezone.utc)
        job = JobRecord(
            job_id=job_id, message_id=mid, status="processing",
            inbound=_inbound(mid), created_at=now, updated_at=now,
        )
        await store.save_job(job)
        loaded = await store.load_job(mid)
        assert loaded is not None
        assert loaded.job_id == job_id
        assert loaded.status == "processing"
        assert loaded.inbound.message_id == mid
        assert loaded.inbound.subject == "docs"  # jsonb inbound round-tripped

        # ── status update by job_id ────────────────────────────────────────
        await store.set_status(job_id, "done")
        assert (await store.load_job(mid)).status == "done"

        # ── thread upsert / get / overwrite ────────────────────────────────
        assert await store.get_thread(tid) is None
        await store.upsert_thread(
            ThreadContext(thread_id=tid, last_matter_number="M12205",
                          last_document_type="Exhibits", updated_at=now)
        )
        ctx = await store.get_thread(tid)
        assert ctx.last_matter_number == "M12205" and ctx.last_document_type == "Exhibits"
        await store.upsert_thread(
            ThreadContext(thread_id=tid, last_matter_number="M99999",
                          last_document_type="Transcripts", updated_at=now)
        )
        ctx2 = await store.get_thread(tid)
        assert ctx2.last_matter_number == "M99999"  # overwritten
    finally:
        # Clean up the test rows.
        await client.table("jobs").delete().eq("message_id", mid).execute()
        await client.table("threads").delete().eq("thread_id", tid).execute()


async def test_load_unknown_returns_none(store):
    assert await store.load_job(f"missing-{uuid.uuid4()}") is None
    assert await store.get_thread(f"missing-{uuid.uuid4()}") is None


async def test_concurrent_claims_single_winner(store):
    # The idempotency gate is the DB UNIQUE(message_id) constraint (not an in-process lock).
    import asyncio

    mid = f"sup-conc-{uuid.uuid4()}"
    client = await store._client_()
    try:
        results = await asyncio.gather(*[store.claim_message(mid) for _ in range(8)])
        assert sum(1 for r in results if r) == 1  # exactly one winner
    finally:
        await client.table("jobs").delete().eq("message_id", mid).execute()
