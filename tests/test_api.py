"""Endpoint tests for the FastAPI app: /health, /inbound idempotency + dispatch, /process (§7.1)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    settings = Settings(_env_file=None)  # local: InMemoryStore + FileEmailClient + inline
    app = create_app(settings)
    # Point the file email client's outbox at a temp dir.
    app.state.email.outbox = tmp_path
    tmp_path.mkdir(exist_ok=True)
    return TestClient(app)


def _inbound_payload(message_id="m-1", thread_id="t-1"):
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "from_addr": "user@example.com",
        "to_addr": "agent@agentmail.to",
        "subject": "docs",
        "body_text": "M12205 exhibits",
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_inbound_processes_new_message(client):
    r = client.post("/inbound", json=_inbound_payload())
    assert r.status_code == 200
    # Inline dispatch ran the (skeleton) job to done.
    store = client.app.state.store
    import asyncio

    job = asyncio.run(store.load_job("m-1"))
    assert job is not None
    assert job.status == "done"


def test_inbound_duplicate_is_skipped(client):
    p = _inbound_payload(message_id="dup-1")
    r1 = client.post("/inbound", json=p)
    r2 = client.post("/inbound", json=p)
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Only one job exists; second call short-circuited on the idempotency claim.
    store = client.app.state.store
    assert len(store._jobs) == 1


def test_process_endpoint_runs_job(client):
    # First create a job via /inbound, then re-process it via /process.
    client.post("/inbound", json=_inbound_payload(message_id="proc-1"))
    r = client.post("/process", json={"message_id": "proc-1"})
    assert r.status_code == 200


def test_inbound_malformed_payload_returns_400(client):
    r = client.post("/inbound", json={"subject": "no message id"})
    assert r.status_code == 400


def test_process_malformed_payload_returns_400(client):
    r = client.post("/process", json={"not_message_id": "x"})
    assert r.status_code == 400
