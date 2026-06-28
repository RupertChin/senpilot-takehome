"""Supabase (Postgres) state store (spec §7.9) — the prod implementation.

Same ``Store`` interface as ``InMemoryStore``, selected by ``ENV`` — a pure adapter swap. The
``jobs.message_id`` UNIQUE constraint is the idempotency gate; ``threads`` holds per-conversation
context for the follow-up feature.

Interface reconciliation: our ``Store`` splits ``claim_message(message_id)`` from ``save_job(job)``
(the app generates the ``job_id``), whereas the spec's single-statement
``INSERT … ON CONFLICT DO NOTHING RETURNING`` claims + creates in one go. So here ``claim_message``
inserts a reserving row (placeholder ``inbound``; a unique-violation means already-seen → False),
and ``save_job`` UPDATEs that row by ``message_id`` with the real inbound and the app's ``job_id``.

Security note: this connects with the API key in ``SUPABASE_KEY``. Prod should use the service-role
key (bypasses RLS, stays server-side). The MCP-provisioned dev project uses the publishable/anon key
with RLS disabled on these two server-only tables — see the README decision log.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from supabase import AsyncClient, create_async_client

from app.config import Settings
from app.models import InboundEmail, JobRecord, ThreadContext
from app.observability import get_logger
from app.store.base import Store

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_unique_violation(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code == "23505":
        return True
    text = str(getattr(exc, "message", "") or exc).lower()
    return "duplicate key" in text or "23505" in text


class SupabaseStore(Store):
    def __init__(self, settings: Settings) -> None:
        self._url = settings.supabase_url
        self._key = settings.supabase_key
        self._client: AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _client_(self) -> AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = await create_async_client(self._url, self._key)
        return self._client

    async def claim_message(self, message_id: str) -> bool:
        client = await self._client_()
        try:
            await (
                client.table("jobs")
                .insert({"message_id": message_id, "status": "processing", "inbound": {}})
                .execute()
            )
            return True  # newly claimed
        except Exception as exc:  # noqa: BLE001 — postgrest APIError on the UNIQUE(message_id) gate
            if _is_unique_violation(exc):
                return False  # already seen — idempotent skip
            raise

    async def save_job(self, job: JobRecord) -> None:
        # Upsert on message_id (insert-or-replace, matching the ABC + InMemoryStore). Normally the
        # claim row already exists, so this fills its real inbound + the app's job_id; if save is
        # ever called without a prior claim it still persists, never silently no-ops.
        client = await self._client_()
        await (
            client.table("jobs")
            .upsert(
                {
                    "job_id": str(job.job_id),
                    "message_id": job.message_id,
                    "status": job.status,
                    "inbound": json.loads(job.inbound.model_dump_json()),
                    "created_at": job.created_at.isoformat(),
                    "updated_at": job.updated_at.isoformat(),
                },
                on_conflict="message_id",
            )
            .execute()
        )

    async def load_job(self, message_id: str) -> JobRecord | None:
        client = await self._client_()
        res = (
            await client.table("jobs")
            .select("*")
            .eq("message_id", message_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        # A claimed-but-not-yet-saved row carries a placeholder inbound ({}); treat it as not-found
        # rather than failing to reconstruct InboundEmail. (Not reachable in the normal flow, where
        # save_job immediately follows claim_message.)
        if not rows[0].get("inbound"):
            return None
        return _row_to_job(rows[0])

    async def set_status(self, job_id: str, status: str) -> None:
        client = await self._client_()
        await (
            client.table("jobs")
            .update({"status": status, "updated_at": _now_iso()})
            .eq("job_id", job_id)
            .execute()
        )

    async def get_thread(self, thread_id: str) -> ThreadContext | None:
        client = await self._client_()
        res = (
            await client.table("threads")
            .select("*")
            .eq("thread_id", thread_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        r = rows[0]
        return ThreadContext(
            thread_id=r["thread_id"],
            last_matter_number=r.get("last_matter_number"),
            last_document_type=r.get("last_document_type"),
            updated_at=r["updated_at"],
        )

    async def upsert_thread(self, ctx: ThreadContext) -> None:
        client = await self._client_()
        await (
            client.table("threads")
            .upsert(
                {
                    "thread_id": ctx.thread_id,
                    "last_matter_number": ctx.last_matter_number,
                    "last_document_type": ctx.last_document_type,
                    "updated_at": ctx.updated_at.isoformat(),
                },
                on_conflict="thread_id",
            )
            .execute()
        )


def _row_to_job(row: dict) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        message_id=row["message_id"],
        status=row["status"],
        inbound=InboundEmail(**row["inbound"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
