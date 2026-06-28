"""Job orchestration — ``process_job`` (spec §4 / §7.2 / §9).

Runs an inbound email through the full pipeline: classify → extract → validate → scrape → package
→ summarize → reply. Guarantees exactly one outbound action per job (a reply, an ack, or a silent
drop) and a terminal status write.

Error taxonomy (§9): user errors are the friendly terminal replies (clarification / not-found /
empty-type) handled inline. An infrastructure/transient failure is classified — in ``tasks`` mode
it re-raises so ``/process`` returns 5xx and Cloud Tasks retries; in ``inline`` mode (no queue) it
sends the §5 failure email once (unless a reply already went out) and marks the job failed. A
``finally`` removes the per-job temp files. Thread-context inheritance lands in Stage 8.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings
from app.email.base import EmailClient
from app.errors import RetryableError, TerminalError, classify_exception
from app.llm.base import LLM
from app.models import DocCounts
from app.observability import get_logger, init_sentry, job_context
from app.package.packager import package
from app.replies import clarification, empty_type, failure, matter_not_found
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
                # A one-line ack is the cut-order flex (Stage 9). For now: no reply, mark done.
                log.info("classified_conversational", message_id=message_id)
                await deps.store.set_status(job.job_id, "done")
                return

            # ── extract + validate ────────────────────────────────────────────
            parsed = await llm.extract(inbound)
            # TODO(Stage 8): merge thread context (inherit matter/type when null).
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
            link = payload if delivery == "link" else None
            body = await llm.summarize(
                scrape,
                delivery,
                link,
                link_size_mb=(size / 1_000_000) if delivery == "link" else None,
                link_ttl_hours=settings.signed_url_ttl_hours if delivery == "link" else None,
            )
            await _reply(
                deps, inbound, body, attachment_path=payload if delivery == "attach" else None
            )
            outbound_sent = True

            # TODO(Stage 8): upsert thread context after a successful scrape.
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


async def _reply(deps, inbound, body, attachment_path=None) -> None:
    await deps.email.send_reply(
        in_reply_to=inbound, body=body, attachment_path=attachment_path
    )
