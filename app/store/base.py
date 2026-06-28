"""State store interface (spec §7.9).

One interface backs two uses — idempotency (claim a message once) and thread context (for the
follow-up feature). ``InMemoryStore`` (local) and ``SupabaseStore`` (prod) implement it, selected
by ``ENV``. Both are exercised identically, so the idempotency gate and the thread feature are
built and tested locally with no database; Supabase is a pure adapter swap before deploy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import JobRecord, ThreadContext


class Store(ABC):
    """Idempotency + job persistence + per-thread context."""

    @abstractmethod
    async def claim_message(self, message_id: str) -> bool:
        """Atomically claim a message. ``True`` if newly claimed (process it); ``False`` if seen."""
        ...

    @abstractmethod
    async def save_job(self, job: JobRecord) -> None:
        """Persist (insert or replace) a job record."""
        ...

    @abstractmethod
    async def load_job(self, message_id: str) -> JobRecord | None:
        """Load the job record for a message id, or ``None``."""
        ...

    @abstractmethod
    async def set_status(self, job_id: str, status: str) -> None:
        """Update a job's status (and ``updated_at``)."""
        ...

    @abstractmethod
    async def get_thread(self, thread_id: str) -> ThreadContext | None:
        """Return the stored context for a thread, or ``None``."""
        ...

    @abstractmethod
    async def upsert_thread(self, ctx: ThreadContext) -> None:
        """Insert or update a thread's context."""
        ...
