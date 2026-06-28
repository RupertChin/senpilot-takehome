"""FastAPI app: ``/inbound``, ``/process``, ``/health`` (spec §7.1).

Webhook ingestion, idempotency claim, and dispatch. Provider-specific concerns that need real
accounts are stubbed with clearly-marked TODOs until their stage:
  - AgentMail signature verification on ``/inbound`` — Stage 11.
  - Cloud Tasks OIDC verification on ``/process`` — Stage 12.

Dependencies (store, email client, llm) live on ``app.state`` so tests can override them.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response

from app.config import Settings, get_settings
from app.deps import build_email_client, build_llm, build_store
from app.errors import RetryableError, TerminalError, classify_exception
from app.models import JobRecord
from app.observability import configure_logging, get_logger, init_sentry
from app.pipeline import PipelineDeps, process_job
from app.queue.tasks import enqueue

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    init_sentry(settings)

    app = FastAPI(title="Senpilot Regulatory Email Agent")
    app.state.settings = settings
    app.state.store = build_store(settings)
    app.state.email = build_email_client(settings)
    app.state.llm = build_llm(settings)

    def _deps() -> PipelineDeps:
        return PipelineDeps(store=app.state.store, email=app.state.email, llm=app.state.llm)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/inbound")
    async def inbound(request: Request) -> Response:
        # TODO(Stage 11): verify the AgentMail webhook signature; reject 401 if invalid.
        try:
            raw = await request.json()
            email = await app.state.email.parse_inbound(raw)
        except Exception:  # noqa: BLE001 — malformed/unparseable body is a client error
            log.warning("inbound_parse_failed", exc_info=True)
            return Response(status_code=400)

        newly_claimed = await app.state.store.claim_message(email.message_id)
        if not newly_claimed:
            log.info("duplicate_skipped", message_id=email.message_id)
            return Response(status_code=200)

        now = datetime.now(timezone.utc)
        job = JobRecord(
            job_id=str(uuid.uuid4()),
            message_id=email.message_id,
            status="processing",
            inbound=email,
            created_at=now,
            updated_at=now,
        )
        await app.state.store.save_job(job)

        deps = _deps()

        async def _run(mid: str) -> None:
            await process_job(mid, deps=deps)

        try:
            await enqueue(email.message_id, settings=settings, process_fn=_run)
        except Exception:  # noqa: BLE001 — inline mode runs the job here
            # process_job is contractually self-terminating (§7.2); if something still escapes,
            # mark the job failed and 200 the webhook. Returning 5xx would make AgentMail retry,
            # but the idempotency claim is already consumed, so the retry would be skipped and the
            # job lost. The user-facing failure reply is owned by process_job (Stage 7).
            log.error("inline_process_failed", message_id=email.message_id, exc_info=True)
            await app.state.store.set_status(job.job_id, "failed")
        return Response(status_code=200)

    @app.post("/process")
    async def process(request: Request) -> Response:
        # TODO(Stage 12): verify the Cloud Tasks OIDC token (audience = service URL).
        try:
            body = await request.json()
            message_id = body["message_id"]
        except Exception:  # noqa: BLE001 — malformed body is a client error
            log.warning("process_parse_failed", exc_info=True)
            return Response(status_code=400)
        deps = _deps()
        try:
            await process_job(message_id, deps=deps)
        except TerminalError:
            # Already handled with a user reply — do not let Cloud Tasks retry.
            return Response(status_code=200)
        except RetryableError:
            # Let Cloud Tasks retry.
            return Response(status_code=503)
        except Exception as exc:  # noqa: BLE001 — classify then map to the right status
            classified = classify_exception(exc)
            status = 200 if isinstance(classified, TerminalError) else 503
            log.error("process_failed", message_id=message_id, status=status, exc_info=True)
            return Response(status_code=status)
        return Response(status_code=200)

    return app


app = create_app()
