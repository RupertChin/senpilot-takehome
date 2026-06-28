"""Job orchestration — ``process_job`` (spec §4 / §7.2).

Runs an inbound email through the full pipeline: classify → extract → validate → scrape → package
→ summarize → reply. Guarantees exactly one outbound action per job (a reply, an ack, or a silent
drop) and a terminal status write. Stage 6 wires the inline happy path + the §5 terminal-but-
friendly branches (clarification / not-found / empty-type); the failure-email + retry taxonomy and
the GCS link branch are hardened in Stage 7, thread-context inheritance in Stage 8.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.email.base import EmailClient
from app.errors import RetryableError
from app.llm.base import LLM
from app.models import DocCounts
from app.observability import get_logger, job_context
from app.package.packager import package
from app.replies import clarification, empty_type, matter_not_found
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

        # ── classify ──────────────────────────────────────────────────────────
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

        # ── extract + validate ────────────────────────────────────────────────
        parsed = await llm.extract(inbound)
        # TODO(Stage 8): merge thread context (inherit matter/type when null).
        if parsed.matter_number is None or parsed.document_type is None:
            body = clarification(
                missing_matter=parsed.matter_number is None,
                missing_type=parsed.document_type is None,
            )
            await _reply(deps, inbound, body)
            await deps.store.set_status(job.job_id, "done")
            return

        matter = parsed.matter_number
        doc_type = parsed.document_type

        # ── scrape ────────────────────────────────────────────────────────────
        scrape = await scrape_matter(
            matter, doc_type, settings=settings, extract_metadata=llm.extract_metadata
        )
        if not scrape.found:
            await _reply(deps, inbound, matter_not_found(matter))
            await deps.store.set_status(job.job_id, "done")
            return
        if scrape.type_count == 0:
            counts = scrape.metadata.counts if scrape.metadata else DocCounts()
            await _reply(deps, inbound, empty_type(matter, doc_type, counts))
            await deps.store.set_status(job.job_id, "done")
            return
        if scrape.downloaded == 0:
            # The type has documents but NONE downloaded after per-doc retries — an infra failure,
            # not a user error. Raise (retryable) rather than ship an empty ZIP + "0 of N" reply.
            # The failure email that surfaces this lands in Stage 7.
            raise RetryableError(
                f"all {scrape.failed} {doc_type} downloads failed for {matter}"
            )

        # ── package → summarize → reply ───────────────────────────────────────
        delivery, payload = await package(scrape.documents, job.job_id, settings)
        link = payload if delivery == "link" else None
        body = await llm.summarize(
            scrape,
            delivery,
            link,
            link_ttl_hours=settings.signed_url_ttl_hours if delivery == "link" else None,
        )
        attachment = payload if delivery == "attach" else None
        await _reply(deps, inbound, body, attachment_path=attachment)

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


async def _reply(deps, inbound, body, attachment_path=None) -> None:
    await deps.email.send_reply(
        in_reply_to=inbound, body=body, attachment_path=attachment_path
    )
