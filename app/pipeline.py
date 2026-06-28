"""Job orchestration — ``process_job`` (spec §4 / §7.2 / §9).

Runs an inbound email through the full pipeline: classify → extract → validate → scrape → package
→ summarize → reply. Guarantees exactly one outbound action per job (a reply, an ack, or a silent
drop) and a terminal status write.

Error taxonomy (§9): user errors are the friendly terminal replies (clarification / not-found /
empty-type) handled inline. An infrastructure/transient failure is classified — in ``tasks`` mode
it re-raises so ``/process`` returns 5xx and Cloud Tasks retries; in ``inline`` mode (no queue) it
sends the §5 failure email once (unless a reply already went out) and marks the job failed. A
``finally`` removes the per-job temp files. Thread-context inheritance (§7.9) merges on extract and
upserts after a successful scrape.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings, get_settings
from app.email.base import EmailClient
from app.errors import RetryableError, TerminalError, classify_exception
from app.llm.base import LLM
from app.models import DocCounts, ParsedRequest, ThreadContext
from app.observability import get_logger, init_sentry, job_context
from app.package.packager import package
from app.replies import clarification, conversational_ack, empty_type, failure, matter_not_found
from app.scrape.scraper import scrape_matter
from app.store.base import Store

log = get_logger(__name__)


@dataclass
class PipelineDeps:
    """The collaborators ``process_job`` needs, injected so they can be swapped per environment."""

    store: Store
    email: EmailClient
    llm: LLM | None = None
    settings: Settings | None = None

    def resolved_settings(self) -> Settings:
        return self.settings or get_settings()


async def process_job(message_id: str, *, deps: PipelineDeps) -> None:
    """Run the job for ``message_id`` to a terminal status (spec §4)."""
    job = await deps.store.load_job(message_id)
    if job is None:
        log.warning("job_not_found", message_id=message_id)
        return

    with job_context(job.job_id):
        inbound = job.inbound
        settings = deps.resolved_settings()
        llm = deps.llm
        if llm is None:  # pragma: no cover — wired in every real environment
            raise RuntimeError("process_job requires an LLM (Stage 5+)")

        log.info("job_received", message_id=message_id, thread_id=inbound.thread_id)
        matter: str | None = None
        outbound_sent = False
        cleanup: list[Path] = []

        try:
            # ── classify ──────────────────────────────────────────────────────
            label = await llm.classify(inbound)
            if label == "junk":
                log.info("classified_junk_drop", message_id=message_id)
                await deps.store.set_status(job.job_id, "done")
                return
            if label == "conversational":
                # A one-line friendly ack for a clear thanks/greeting (§4).
                log.info("classified_conversational", message_id=message_id)
                await _reply(deps, inbound, conversational_ack())
                outbound_sent = True
                await deps.store.set_status(job.job_id, "done")
                return

            # ── extract + validate ────────────────────────────────────────────
            parsed = await llm.extract(inbound)
            parsed = await _merge_thread_context(deps, inbound.thread_id, parsed)
            if parsed.matter_number is None or parsed.document_type is None:
                await _reply(
                    deps,
                    inbound,
                    clarification(
                        missing_matter=parsed.matter_number is None,
                        missing_type=parsed.document_type is None,
                    ),
                )
                outbound_sent = True
                await deps.store.set_status(job.job_id, "done")
                return

            matter = parsed.matter_number
            doc_type = parsed.document_type

            # ── scrape ────────────────────────────────────────────────────────
            scrape = await scrape_matter(
                matter, doc_type, settings=settings, extract_metadata=llm.extract_metadata
            )
            for d in scrape.documents:
                for p in d.paths:
                    cleanup.append(Path(p).parent)

            if not scrape.found:
                await _reply(deps, inbound, matter_not_found(matter))
                outbound_sent = True
                await deps.store.set_status(job.job_id, "done")
                return
            if scrape.type_count == 0:
                counts = scrape.metadata.counts if scrape.metadata else DocCounts()
                await _reply(deps, inbound, empty_type(matter, doc_type, counts))
                outbound_sent = True
                # The matter is valid — remember it so a follow-up ("then the Exhibits") inherits it.
                await _upsert_thread(deps, inbound.thread_id, matter, doc_type)
                await deps.store.set_status(job.job_id, "done")
                return
            if scrape.downloaded == 0:
                # Type has documents but none downloaded after per-doc retries — an infra failure,
                # not a user error. Raise (retryable) rather than ship an empty ZIP + "0 of N".
                raise RetryableError(
                    f"all {scrape.failed} {doc_type} downloads failed for {matter}"
                )

            # ── package → summarize → reply ────────────────────────────────────
            delivery, payload, size = await package(scrape.documents, job.job_id, settings)
            if delivery == "attach":
                cleanup.append(Path(payload))
            # "attach" and "url_attach" both deliver a real attachment (inline vs fetched-by-URL);
            # only "link" puts the file behind a link in the body.
            is_link = delivery == "link"
            link = payload if is_link else None
            body = await llm.summarize(
                scrape,
                "link" if is_link else "attach",
                link,
                link_size_mb=(size / 1_000_000) if is_link else None,
                link_ttl_hours=settings.signed_url_ttl_hours if is_link else None,
            )
            await _reply(
                deps,
                inbound,
                body,
                attachment_path=payload if delivery == "attach" else None,
                attachment_url=payload if delivery == "url_attach" else None,
                attachment_filename=f"{matter}_{doc_type}.zip".replace(" ", "_")
                if delivery == "url_attach"
                else None,
            )
            outbound_sent = True

            # Remember this matter+type for follow-ups in the same thread (§7.9).
            await _upsert_thread(deps, inbound.thread_id, matter, doc_type)
            await deps.store.set_status(job.job_id, "done")
            log.info(
                "job_done",
                message_id=message_id,
                matter=matter,
                downloaded=scrape.downloaded,
                requested=scrape.requested,
                delivery=delivery,
            )

        except TerminalError:
            # A raised terminal user-error (not one of the friendly branches above; currently
            # defensive — no stage raises this today). Mark the job terminal BEFORE re-raising so
            # it can't be left "processing" in tasks mode (where /process returns 200 and does no
            # status write). Re-raise so /process does not retry a user error.
            log.warning("job_terminal_error", message_id=message_id, exc_info=True)
            await deps.store.set_status(job.job_id, "failed")
            raise
        except Exception as exc:  # noqa: BLE001 — classify and decide retry vs failure email
            classified = classify_exception(exc)
            if isinstance(classified, RetryableError) and settings.queue_mode == "tasks":
                # Let Cloud Tasks retry the whole job via a 5xx at /process (Stage 12).
                log.warning("job_retryable_reraise", message_id=message_id, exc_info=True)
                raise
            log.error(
                "job_failed",
                message_id=message_id,
                matter=matter,
                error=str(exc).splitlines()[0],
                exc_info=True,
            )
            init_sentry(settings)  # no-op without a DSN
            if not outbound_sent:
                try:
                    await _reply(deps, inbound, failure(matter, job.job_id))
                except Exception:  # noqa: BLE001 — best-effort; never raise from the failure path
                    log.error("failure_email_send_failed", message_id=message_id, exc_info=True)
            await deps.store.set_status(job.job_id, "failed")
        finally:
            _cleanup(cleanup)


def _cleanup(paths: list[Path]) -> None:
    """Best-effort removal of per-job temp files/dirs (the downloaded files + the local zip)."""
    for p in set(paths):
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except OSError:  # pragma: no cover — cleanup is best-effort
            pass


async def _merge_thread_context(
    deps: PipelineDeps, thread_id: str | None, parsed: ParsedRequest
) -> ParsedRequest:
    """Inherit a null matter/type from the thread's last request (spec §7.9).

    Explicit values in the current email ALWAYS win — inheritance fills only the null fields. Sets
    the ``inherited_*`` flags for logging. No-op when there's no thread or no stored context.
    """
    if thread_id is None:
        return parsed
    ctx = await deps.store.get_thread(thread_id)
    if ctx is None:
        return parsed

    matter = parsed.matter_number
    doc_type = parsed.document_type
    inherited_matter = inherited_type = False
    if matter is None and ctx.last_matter_number is not None:
        matter = ctx.last_matter_number
        inherited_matter = True
    if doc_type is None and ctx.last_document_type is not None:
        doc_type = ctx.last_document_type
        inherited_type = True

    if inherited_matter or inherited_type:
        log.info(
            "thread_context_inherited",
            thread_id=thread_id,
            inherited_matter=inherited_matter,
            inherited_type=inherited_type,
        )
    return ParsedRequest(
        matter_number=matter,
        document_type=doc_type,
        inherited_matter=inherited_matter,
        inherited_type=inherited_type,
    )


async def _upsert_thread(
    deps: PipelineDeps, thread_id: str | None, matter: str, doc_type: str
) -> None:
    """Persist the matter+type as this thread's last request, for future follow-ups (§7.9).

    Best-effort: this runs AFTER the user reply has already been sent, so a store hiccup must never
    fail the job (which, in tasks mode, would retry and re-send the documents). Remembering the
    thread is a convenience, not a delivery guarantee — log and move on if it fails.
    """
    if thread_id is None:
        return
    try:
        await deps.store.upsert_thread(
            ThreadContext(
                thread_id=thread_id,
                last_matter_number=matter,
                last_document_type=doc_type,
                updated_at=datetime.now(timezone.utc),
            )
        )
    except Exception:  # noqa: BLE001 — never fail a delivered job on thread bookkeeping
        log.warning("thread_upsert_failed", thread_id=thread_id, exc_info=True)


async def _reply(
    deps, inbound, body, attachment_path=None, attachment_url=None, attachment_filename=None
) -> None:
    await deps.email.send_reply(
        in_reply_to=inbound,
        body=body,
        attachment_path=attachment_path,
        attachment_url=attachment_url,
        attachment_filename=attachment_filename,
    )
