"""Endpoint tests for the FastAPI app: /health, /inbound idempotency + dispatch, /process (§7.1)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class _JunkLLM:
    """A network-free LLM stub: classifies everything as junk so process_job marks done with no
    extract/scrape. Keeps the API tests focused on webhook/idempotency/dispatch wiring."""

    async def classify(self, email):
        return "junk"

    async def extract(self, email):  # pragma: no cover — not reached for junk
        raise AssertionError

    async def extract_metadata(self, matter, text):  # pragma: no cover
        raise AssertionError

    async def summarize(self, *a, **k):  # pragma: no cover
        raise AssertionError


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    settings = Settings(_env_file=None)  # local: InMemoryStore + FileEmailClient + inline
    app = create_app(settings)
    # Point the file email client's outbox at a temp dir; inject a network-free LLM.
    app.state.email.outbox = tmp_path
    app.state.llm = _JunkLLM()
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


def test_inbound_bad_signature_returns_401(client):
    # Swap in an email client that rejects the signature (the one case /inbound 401s, not 200s).
    class _RejectingEmail:
        def verify_signature(self, raw_body, headers):
            return False

        async def parse_inbound(self, raw):  # pragma: no cover — never reached on a 401
            raise AssertionError("parse must not run when the signature is rejected")

    client.app.state.email = _RejectingEmail()
    r = client.post("/inbound", json=_inbound_payload(), headers={"x-agentmail-signature": "bad"})
    assert r.status_code == 401
    # No job was claimed (we rejected before processing).
    assert len(client.app.state.store._jobs) == 0


def test_inbound_ignores_non_received_event(client):
    # A non-message.received AgentMail event is acknowledged (200) but not processed.
    r = client.post("/inbound", json={"event_type": "message.sent", "message": {"message_id": "x"}})
    assert r.status_code == 200
    assert len(client.app.state.store._jobs) == 0


def test_process_tasks_mode_rejects_without_oidc(monkeypatch):
    # In tasks mode /process requires a valid OIDC token; a tokenless call -> 401 (fail closed).
    settings = Settings(env="prod", queue_mode="tasks", process_url="https://x/process",
                        tasks_invoker_sa="invoker@p.iam.gserviceaccount.com", _env_file=None)
    app = create_app(settings)
    # Stub the deps so create_app doesn't need live Supabase/AgentMail.
    app.state.store = type("S", (), {})()
    c = TestClient(app)
    r = c.post("/process", json={"message_id": "m"})  # no Authorization header
    assert r.status_code == 401
