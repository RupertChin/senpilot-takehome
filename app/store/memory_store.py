"""In-memory state store (spec §7.9) — the local default.

Plain dicts guarded by an ``asyncio.Lock``: one keyed by ``message_id`` (jobs + idempotency),
one keyed by ``thread_id`` (thread context). ``claim_message`` is an atomic check-and-set — the
idempotency gate. Not durable across restarts (fine for the dev loop), and fully exercises the
idempotency logic and the thread follow-up feature with no database. Mirrors the semantics of the
Supabase ``INSERT ... ON CONFLICT DO NOTHING`` claim.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.models import JobRecord, ThreadContext
from app.store.base import Store


class InMemoryStore(Store):
    def __init__(self) -> None:
        # message_id -> JobRecord (or None while claimed-but-not-yet-saved).
        self._jobs: dict[str, JobRecord | None] = {}
        # job_id -> message_id, so set_status can find the record.
        self._job_index: dict[str, str] = {}
        self._threads: dict[str, ThreadContext] = {}
        self._lock = asyncio.Lock()

    async def claim_message(self, message_id: str) -> bool:
        # NOTE: this reserves the slot (job=None) and the caller persists the full JobRecord via
        # save_job — a two-step claim. The prod Supabase path does both in one atomic
        # INSERT ... ON CONFLICT DO NOTHING RETURNING (so SupabaseStore.save_job reconciles via
        # UPDATE/upsert, not a second INSERT). For single-process local dev this split is fine: a
        # crash between claim and save wipes all in-memory state anyway (not durable).
        async with self._lock:
            if message_id in self._jobs:
                return False  # already seen — skip (idempotent)
            self._jobs[message_id] = None  # reserve the slot
            return True

    async def save_job(self, job: JobRecord) -> None:
        async with self._lock:
            self._jobs[job.message_id] = job
            self._job_index[job.job_id] = job.message_id

    async def load_job(self, message_id: str) -> JobRecord | None:
        async with self._lock:
            return self._jobs.get(message_id)

    async def set_status(self, job_id: str, status: str) -> None:
        async with self._lock:
            message_id = self._job_index.get(job_id)
            if message_id is None:
                return
            job = self._jobs.get(message_id)
            if job is None:
                return
            self._jobs[message_id] = job.model_copy(
                update={"status": status, "updated_at": datetime.now(timezone.utc)}
            )

    async def get_thread(self, thread_id: str) -> ThreadContext | None:
        async with self._lock:
            return self._threads.get(thread_id)

    async def upsert_thread(self, ctx: ThreadContext) -> None:
        async with self._lock:
            self._threads[ctx.thread_id] = ctx
