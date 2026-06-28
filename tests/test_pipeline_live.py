"""Live end-to-end test (spec §12 definition of done): an InboundEmail fixture through process_job
with the real LLM + real scraper produces the correct reply + ZIP attachment in the outbox.

Marked live+llm (hits the gov site AND Anthropic). Caps downloads to 3 for politeness.
"""

from __future__ import annotations

import zipfile
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.email.file import FileEmailClient
from app.llm.anthropic_client import AnthropicLLM
from app.models import InboundEmail, JobRecord
from app.pipeline import PipelineDeps, process_job
from app.store.memory_store import InMemoryStore

pytestmark = [pytest.mark.live, pytest.mark.llm]


async def test_m12205_end_to_end_attach(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV", "prod")  # headless scraper
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setenv("MAX_DOCUMENTS", "3")  # politeness — exercise the path with fewer downloads
    settings = Settings()  # reads .env for ANTHROPIC_API_KEY
    if not settings.anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY not set")

    outbox = tmp_path / "outbox"
    deps = PipelineDeps(
        store=InMemoryStore(),
        email=FileEmailClient(outbox_dir=outbox),
        llm=AnthropicLLM(settings),
        settings=settings,
    )

    inbound = InboundEmail(
        message_id="e2e-1",
        thread_id="e2e-thread",
        from_addr="user@example.com",
        to_addr="agent@agentmail.to",
        subject="Documents please",
        body_text="Hi, could you send me the Other Documents for matter M12205? Thanks!",
        received_at=datetime(2026, 6, 28, tzinfo=timezone.utc),
    )
    now = datetime.now(timezone.utc)
    await deps.store.claim_message(inbound.message_id)
    await deps.store.save_job(
        JobRecord(
            job_id="e2e-job",
            message_id="e2e-1",
            status="processing",
            inbound=inbound,
            created_at=now,
            updated_at=now,
        )
    )

    await process_job("e2e-1", deps=deps)

    assert (await deps.store.load_job("e2e-1")).status == "done"

    emls = list(outbox.glob("*.eml"))
    assert len(emls) == 1
    body = emls[0].read_text()
    # Success body invariants (numbers authoritative — matter + k-of-N preserved).
    assert "M12205" in body
    assert "Other Documents" in body
    assert "of the" in body  # "{k} of the {N}"
    assert "X-Attachment:" in body

    zips = list(outbox.glob("*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as zf:
        names = zf.namelist()
        assert 1 <= len(names) <= 3  # capped at MAX_DOCUMENTS=3
        assert len(set(names)) == len(names)  # no duplicate entries
