"""Offline end-to-end pipeline tests (spec §4, §12).

A fake LLM and a patched ``scrape_matter`` exercise every process_job branch deterministically (no
network): junk drop, conversational drop, clarification (missing matter/type/both), matter-not-
found, empty-type, and the happy attach path producing a reply + ZIP in the outbox.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.email.file import FileEmailClient
from app.models import (
    DocCounts,
    DownloadedDoc,
    InboundEmail,
    JobRecord,
    MatterMetadata,
    ParsedRequest,
    ScrapeResult,
)
from app.pipeline import PipelineDeps, process_job
from app.store.memory_store import InMemoryStore


def _inbound(message_id="m-1", body="M12205 Other Documents"):
    return InboundEmail(
        message_id=message_id,
        thread_id="t-1",
        from_addr="user@example.com",
        to_addr="agent@agentmail.to",
        subject="docs",
        body_text=body,
        received_at=datetime(2026, 6, 28, tzinfo=timezone.utc),
    )


class FakeLLM:
    """Configurable LLM stub."""

    def __init__(self, *, label="request", parsed=None, summary="SUMMARY"):
        self._label = label
        self._parsed = parsed or ParsedRequest(
            matter_number="M12205", document_type="Other Documents"
        )
        self._summary = summary

    async def classify(self, email):
        return self._label

    async def extract(self, email):
        return self._parsed

    async def extract_metadata(self, matter, text):
        return MatterMetadata(matter_number=matter)

    async def summarize(self, scrape, delivery, link, *, link_size_mb=None, link_ttl_hours=None):
        return self._summary


def _metadata():
    return MatterMetadata(
        matter_number="M12205",
        organization="Halifax Regional Water Commission",
        project="Windsor Street Exchange Redevelopment Project",
        amount="$69,275,000",
        type="Capital Expenditure Approvals",
        category="Water",
        status="Awaiting Compliance",
        date_initial="04/07/2025",
        date_final="10/23/2025",
        counts=DocCounts(exhibits=13, key_documents=5, other_documents=42),
    )


async def _run(deps: PipelineDeps, inbound: InboundEmail):
    now = datetime.now(timezone.utc)
    job = JobRecord(
        job_id="job-1",
        message_id=inbound.message_id,
        status="processing",
        inbound=inbound,
        created_at=now,
        updated_at=now,
    )
    await deps.store.claim_message(inbound.message_id)
    await deps.store.save_job(job)
    await process_job(inbound.message_id, deps=deps)
    return job


def _deps(tmp_path, llm):
    return PipelineDeps(
        store=InMemoryStore(),
        email=FileEmailClient(outbox_dir=tmp_path),
        llm=llm,
        settings=Settings(_env_file=None),
    )


def _outbox_eml(tmp_path):
    files = list(tmp_path.glob("*.eml"))
    return files[0].read_text() if files else None


# ── classify branches ─────────────────────────────────────────────────────────


async def test_junk_is_silently_dropped(tmp_path):
    deps = _deps(tmp_path, FakeLLM(label="junk"))
    job = await _run(deps, _inbound())
    assert _outbox_eml(tmp_path) is None  # no reply
    assert (await deps.store.load_job("m-1")).status == "done"


async def test_conversational_gets_one_line_ack(tmp_path):
    deps = _deps(tmp_path, FakeLLM(label="conversational"))
    await _run(deps, _inbound(body="thanks!"))
    body = _outbox_eml(tmp_path)
    assert body is not None and "Happy to help!" in body
    assert (await deps.store.load_job("m-1")).status == "done"


# ── clarification branches ────────────────────────────────────────────────────


async def test_job_not_found_is_noop(tmp_path):
    deps = _deps(tmp_path, FakeLLM())
    await process_job("ghost", deps=deps)  # no job saved -> early return, no raise
    assert _outbox_eml(tmp_path) is None


async def test_clarification_missing_matter(tmp_path):
    llm = FakeLLM(parsed=ParsedRequest(matter_number=None, document_type="Exhibits"))
    deps = _deps(tmp_path, llm)
    await _run(deps, _inbound())
    body = _outbox_eml(tmp_path)
    assert "I need a matter number (like M12205)" in body
    assert (await deps.store.load_job("m-1")).status == "done"  # terminal status written


async def test_clarification_missing_type(tmp_path):
    llm = FakeLLM(parsed=ParsedRequest(matter_number="M12205", document_type=None))
    deps = _deps(tmp_path, llm)
    await _run(deps, _inbound())
    body = _outbox_eml(tmp_path)
    assert "Which document type would you like" in body


async def test_clarification_both_missing(tmp_path):
    llm = FakeLLM(parsed=ParsedRequest(matter_number=None, document_type=None))
    deps = _deps(tmp_path, llm)
    await _run(deps, _inbound())
    body = _outbox_eml(tmp_path)
    assert "I need a matter number" in body and "Which document type" in body


# ── scrape branches (patched scrape_matter) ───────────────────────────────────


async def test_matter_not_found(tmp_path, monkeypatch):
    async def fake_scrape(matter, doc_type, *, settings, extract_metadata):
        return ScrapeResult(
            matter_number=matter, found=False, requested_type=doc_type, type_count=0
        )

    monkeypatch.setattr("app.pipeline.scrape_matter", fake_scrape)
    deps = _deps(tmp_path, FakeLLM())
    await _run(deps, _inbound())
    body = _outbox_eml(tmp_path)
    assert "I couldn't find matter M12205" in body
    assert (await deps.store.load_job("m-1")).status == "done"


async def test_empty_type(tmp_path, monkeypatch):
    async def fake_scrape(matter, doc_type, *, settings, extract_metadata):
        md = _metadata()
        md.counts = DocCounts(exhibits=13, key_documents=5, other_documents=42, transcripts=0)
        return ScrapeResult(
            matter_number=matter,
            found=True,
            metadata=md,
            requested_type=doc_type,
            type_count=0,
        )

    monkeypatch.setattr("app.pipeline.scrape_matter", fake_scrape)
    llm = FakeLLM(parsed=ParsedRequest(matter_number="M12205", document_type="Transcripts"))
    deps = _deps(tmp_path, llm)
    await _run(deps, _inbound())
    body = _outbox_eml(tmp_path)
    assert "Matter M12205 has 0 Transcripts" in body
    assert "13 Exhibits, 5 Key Documents, 42 Other Documents" in body


# ── happy path (attach) ───────────────────────────────────────────────────────


async def test_happy_path_attaches_zip(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))  # zip lands here so cleanup is assertable
    # Two real temp PDFs for the packager to zip.
    src_dir = tmp_path / "dl"
    src_dir.mkdir()
    docs = []
    for i in range(2):
        p = src_dir / f"H-{i}.pdf"
        p.write_bytes(b"%PDF-1.7 fake pdf " + bytes(200))
        docs.append(
            DownloadedDoc(doc_no=f"H-{i}", filenames=[p.name], paths=[str(p)], total_bytes=p.stat().st_size)
        )

    async def fake_scrape(matter, doc_type, *, settings, extract_metadata):
        return ScrapeResult(
            matter_number=matter,
            found=True,
            metadata=_metadata(),
            requested_type=doc_type,
            type_count=42,
            documents=docs,
            requested=2,
            downloaded=2,
            failed=0,
        )

    monkeypatch.setattr("app.pipeline.scrape_matter", fake_scrape)
    deps = _deps(tmp_path / "out", FakeLLM(summary="Hi, M12205 — I downloaded 2 of the 2 Other Documents."))
    await _run(deps, _inbound())

    out = tmp_path / "out"
    eml = list(out.glob("*.eml"))[0].read_text()
    assert "I downloaded 2 of the 2 Other Documents" in eml
    assert "X-Attachment:" in eml
    # The attachment is the zip, beside the .eml.
    zips = list(out.glob("*.zip"))
    assert len(zips) == 1
    import zipfile

    with zipfile.ZipFile(zips[0]) as zf:
        assert len(zf.namelist()) == 2  # both docs zipped
    # Sources were deleted as they were zipped (peak-disk cap).
    assert not (src_dir / "H-0.pdf").exists()
    # The per-job download dir and the local zip are cleaned up after a successful send.
    assert not src_dir.exists()  # _cleanup rmtree'd the doc parent dir
    assert not (tmp_path / "job-1.zip").exists()  # local zip removed (TMPDIR=tmp_path)


async def test_reply_already_sent_no_double_send(tmp_path, monkeypatch):
    # If the reply succeeds but a later step (set_status) raises, the failure email must NOT fire.
    src = tmp_path / "dl"
    src.mkdir()
    p = src / "H-0.pdf"
    p.write_bytes(b"%PDF-1.7 " + bytes(100))
    doc = DownloadedDoc(doc_no="H-0", filenames=[p.name], paths=[str(p)], total_bytes=p.stat().st_size)

    async def fake_scrape(matter, doc_type, *, settings, extract_metadata):
        return ScrapeResult(
            matter_number=matter, found=True, metadata=_metadata(), requested_type=doc_type,
            type_count=42, documents=[doc], requested=1, downloaded=1, failed=0,
        )

    monkeypatch.setattr("app.pipeline.scrape_matter", fake_scrape)
    store = InMemoryStore()
    deps = PipelineDeps(
        store=store, email=FileEmailClient(outbox_dir=tmp_path / "out"),
        llm=FakeLLM(summary="Hi M12205, 1 of the 1."), settings=Settings(_env_file=None),
    )
    # Make the FINAL set_status("done") raise, after the reply has been sent.
    calls = {"n": 0}
    orig = store.set_status

    async def flaky_set_status(job_id, status):
        calls["n"] += 1
        if status == "done":
            raise RuntimeError("store blip after reply")
        return await orig(job_id, status)

    monkeypatch.setattr(store, "set_status", flaky_set_status)
    await _run(deps, _inbound())
    out = tmp_path / "out"
    emls = list(out.glob("*.eml"))
    assert len(emls) == 1  # exactly one reply — the success body, NOT a second failure email
    assert "Something went wrong" not in emls[0].read_text()


async def test_partial_success_still_replies(tmp_path, monkeypatch):
    # downloaded < requested with failed>0 is NOT an error — it still packages + replies.
    src = tmp_path / "dl"
    src.mkdir()
    p = src / "H-0.pdf"
    p.write_bytes(b"%PDF-1.7 " + bytes(200))
    doc = DownloadedDoc(doc_no="H-0", filenames=[p.name], paths=[str(p)], total_bytes=p.stat().st_size)

    async def fake_scrape(matter, doc_type, *, settings, extract_metadata):
        return ScrapeResult(
            matter_number=matter,
            found=True,
            metadata=_metadata(),
            requested_type=doc_type,
            type_count=42,
            documents=[doc],
            requested=3,
            downloaded=1,
            failed=2,
        )

    monkeypatch.setattr("app.pipeline.scrape_matter", fake_scrape)
    deps = _deps(tmp_path / "out", FakeLLM(summary="Partial: 1 of the 3 (2 skipped)."))
    await _run(deps, _inbound())
    out = tmp_path / "out"
    assert list(out.glob("*.eml"))  # a reply was sent
    assert list(out.glob("*.zip"))  # with the 1 successful doc
    assert (await deps.store.load_job("m-1")).status == "done"


async def test_all_downloads_failed_sends_failure_email(tmp_path, monkeypatch):
    # type has docs but none downloaded -> infra failure. Inline mode (no queue) -> §5 failure
    # email + status failed, NOT an empty-zip "0 of N" reply.
    async def fake_scrape(matter, doc_type, *, settings, extract_metadata):
        return ScrapeResult(
            matter_number=matter,
            found=True,
            metadata=_metadata(),
            requested_type=doc_type,
            type_count=42,
            documents=[],
            requested=3,
            downloaded=0,
            failed=3,
        )

    monkeypatch.setattr("app.pipeline.scrape_matter", fake_scrape)
    deps = _deps(tmp_path, FakeLLM())
    await _run(deps, _inbound())
    body = _outbox_eml(tmp_path)
    assert "Something went wrong fetching M12205" in body
    assert "Reference: job-1" in body
    assert "ZIP" not in body  # no empty-zip attachment reply
    assert (await deps.store.load_job("m-1")).status == "failed"


async def test_infra_error_mid_pipeline_sends_failure_email(tmp_path, monkeypatch):
    # A transient error escaping a stage (inline mode) -> failure email + status failed.
    async def boom_scrape(matter, doc_type, *, settings, extract_metadata):
        raise TimeoutError("scrape timed out")

    monkeypatch.setattr("app.pipeline.scrape_matter", boom_scrape)
    deps = _deps(tmp_path, FakeLLM())
    await _run(deps, _inbound())
    body = _outbox_eml(tmp_path)
    assert "Something went wrong fetching M12205" in body
    assert "Reference: job-1" in body
    assert (await deps.store.load_job("m-1")).status == "failed"


async def test_tasks_mode_reraises_for_cloud_tasks_retry(tmp_path, monkeypatch):
    # In tasks mode a retryable infra failure re-raises so /process can 5xx (Cloud Tasks retries).
    async def boom_scrape(matter, doc_type, *, settings, extract_metadata):
        raise TimeoutError("scrape timed out")

    monkeypatch.setattr("app.pipeline.scrape_matter", boom_scrape)
    deps = PipelineDeps(
        store=InMemoryStore(),
        email=FileEmailClient(outbox_dir=tmp_path),
        llm=FakeLLM(),
        settings=Settings(env="prod", queue_mode="tasks", _env_file=None),
    )
    with pytest.raises(Exception):  # noqa: B017 — RetryableError re-raised
        await _run(deps, _inbound())
    assert _outbox_eml(tmp_path) is None  # no email in tasks mode (Cloud Tasks retries)


async def test_link_branch_states_size_and_reason(tmp_path, monkeypatch):
    # Force the link branch (both caps tiny -> over max_attachment) and assert the summary path
    # uses link + size.
    src = tmp_path / "dl"
    src.mkdir()
    p = src / "big.pdf"
    p.write_bytes(b"%PDF-1.7 " + bytes(1000))
    doc = DownloadedDoc(doc_no="big", filenames=[p.name], paths=[str(p)], total_bytes=p.stat().st_size)

    async def fake_scrape(matter, doc_type, *, settings, extract_metadata):
        return ScrapeResult(
            matter_number=matter,
            found=True,
            metadata=_metadata(),
            requested_type=doc_type,
            type_count=42,
            documents=[doc],
            requested=1,
            downloaded=1,
            failed=0,
        )

    captured = {}

    class _LinkLLM(FakeLLM):
        async def summarize(self, scrape, delivery, link, *, link_size_mb=None, link_ttl_hours=None):
            captured.update(delivery=delivery, link=link, size_mb=link_size_mb, ttl=link_ttl_hours)
            return f"linked at {link}"

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr("app.pipeline.scrape_matter", fake_scrape)
    deps = PipelineDeps(
        store=InMemoryStore(),
        email=FileEmailClient(outbox_dir=tmp_path / "out"),
        llm=_LinkLLM(),
        # Both caps tiny -> the zip exceeds max_attachment -> body-link branch.
        settings=Settings(attach_threshold_bytes=10, max_attachment_bytes=10, _env_file=None),
    )
    await _run(deps, _inbound())
    assert captured["delivery"] == "link"
    assert captured["link"].startswith("https://storage.local.stub/")
    assert captured["size_mb"] is not None and captured["size_mb"] > 0
    assert captured["ttl"] == 72
    # No file attachment on the link branch.
    eml = list((tmp_path / "out").glob("*.eml"))[0].read_text()
    assert "X-Attachment:" not in eml


async def test_url_attach_branch_delivers_attachment_by_url(tmp_path, monkeypatch):
    # Over the inline cap but under max_attachment -> the file is uploaded and attached BY URL,
    # the summary reads as a normal attachment ("attach"), and no link is surfaced in the body.
    src = tmp_path / "dl"
    src.mkdir()
    p = src / "big.pdf"
    p.write_bytes(b"%PDF-1.7 " + bytes(1000))
    doc = DownloadedDoc(doc_no="big", filenames=[p.name], paths=[str(p)], total_bytes=p.stat().st_size)

    async def fake_scrape(matter, doc_type, *, settings, extract_metadata):
        return ScrapeResult(
            matter_number=matter, found=True, metadata=_metadata(), requested_type=doc_type,
            type_count=42, documents=[doc], requested=1, downloaded=1, failed=0,
        )

    captured = {}

    class _CapLLM(FakeLLM):
        async def summarize(self, scrape, delivery, link, *, link_size_mb=None, link_ttl_hours=None):
            captured.update(delivery=delivery, link=link)
            return "here are your docs"

    sent = {}

    class _CapEmail(FileEmailClient):
        async def send_reply(self, *, in_reply_to, body, attachment_path=None,
                             attachment_url=None, attachment_filename=None):
            sent.update(path=attachment_path, url=attachment_url, filename=attachment_filename)

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr("app.pipeline.scrape_matter", fake_scrape)
    deps = PipelineDeps(
        store=InMemoryStore(),
        email=_CapEmail(outbox_dir=tmp_path / "out"),
        llm=_CapLLM(),
        # Inline cap tiny, max_attachment large -> url_attach branch.
        settings=Settings(attach_threshold_bytes=10, max_attachment_bytes=25_000_000, _env_file=None),
    )
    await _run(deps, _inbound())
    assert captured["delivery"] == "attach" and captured["link"] is None  # reads as an attachment
    assert sent["path"] is None
    assert sent["url"].startswith("https://storage.local.stub/")
    assert sent["filename"].endswith(".zip") and " " not in sent["filename"]
