"""Tests for dispatch, dependency selection, and the process_job skeleton (Stage 2 wiring)."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.deps import build_email_client, build_store
from app.email.file import FileEmailClient
from app.pipeline import PipelineDeps, process_job
from app.queue.tasks import enqueue
from app.store.memory_store import InMemoryStore


def _local_settings(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    return Settings(_env_file=None)


def test_build_local_implementations(monkeypatch):
    s = _local_settings(monkeypatch)
    assert isinstance(build_store(s), InMemoryStore)
    assert isinstance(build_email_client(s), FileEmailClient)


async def test_enqueue_inline_runs_process_fn(monkeypatch):
    s = _local_settings(monkeypatch)
    calls = []

    async def fake(mid):
        calls.append(mid)

    await enqueue("m-1", settings=s, process_fn=fake)
    assert calls == ["m-1"]


async def test_enqueue_tasks_mode_raises(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("QUEUE_MODE", "tasks")
    s = Settings(_env_file=None)

    async def fake(mid):  # pragma: no cover — must not be called
        raise AssertionError("should not dispatch")

    with pytest.raises(NotImplementedError):
        await enqueue("m-1", settings=s, process_fn=fake)


async def test_process_job_unknown_message_is_noop():
    store = InMemoryStore()
    deps = PipelineDeps(store=store, email=FileEmailClient())
    await process_job("ghost", deps=deps)  # must not raise


async def test_claim_without_save_loads_none():
    # claim reserves the slot as None; load before save returns None (same surface as unknown).
    store = InMemoryStore()
    assert await store.claim_message("m-1") is True
    assert await store.load_job("m-1") is None
