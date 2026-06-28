# PLAN — Senpilot Regulatory Email Agent

Living progress tracker. Derived from the spec's build order (§11), subdivided into small,
independently-testable, commit-sized stages. Each stage leaves the system in a working state.

**Mode:** one-time PLAN (this doc) → repeating STAGE loop (implement → self-validate → review team
→ commit). The review team (4 focus areas: spec-conformance, robustness/gotchas, security,
test-adequacy) is the gate between stages — not the human. Hard stops only: the Phase-1
plan/egress confirmation; any stage needing a deferred real account; blocked gov-site egress; a
settled spec decision believed wrong.

## Environment facts (verified at plan time)
- `ANTHROPIC_API_KEY` is **set** in `.env` (the only credential needed for stages 1–9).
- `.gitignore` covers `.env` and `*/.env` — secrets stay out of git.
- System Python is **3.13.1** (spec targets 3.11; async stack is compatible). Build inside `.venv`.
- **Network egress to `uarb.novascotia.ca` is reachable** — a HEAD probe returned the expected
  `307 → ?redirected=true` with the `/fmi` `JSESSIONID` (matches FINDINGS §5.11). Browser-level
  reachability is validated in Stage 3.

## Deferred real accounts (intentionally blank — stages ordered so these come LAST)
- `SUPABASE_*` → **Stage 10** (SupabaseStore swap). Until then: `InMemoryStore`.
- `AGENTMAIL_*` → **Stage 11** (AgentMailClient + real inbound). Until then: `FileEmailClient` → `./outbox/`.
- `GCP_*` / `GCS_*` / `TASKS_*` → **Stage 12** (Cloud Tasks + OIDC + deploy). Until then:
  `QUEUE_MODE=inline`, GCS upload stubbed.

Stages 1–9 run hands-off on the Anthropic key + local substitutes. Stages 10–12 each PAUSE to
request their specific credential.

---

## Stages

### Stage 1 — Scaffold core (config, models, errors, observability) — _account-free_
- **Goal:** project skeleton + the pure, dependency-light core.
- **Files:** `requirements.txt`, `app/__init__.py` + subpackage `__init__.py`s, `app/config.py`
  (§3 Settings, env auto-detect), `app/models.py` (§6 all schemas + validators), `app/errors.py`
  (§9 RetryableError/TerminalError/classify_exception), `app/observability.py` (structlog +
  correlation-id contextvar + sentry init guard).
- **Validation:** `pip install -r requirements.txt`; pytest unit tests — Settings auto-detects
  local/prod and reads tunables; `matter_number` regex `^M\d{4,6}$` (normalize upper/strip);
  `DocumentType` enum; `classify_exception` maps sample exceptions correctly.
- **Parallel subagents:** models+validators ‖ config ‖ errors+observability ‖ test-writing.
- **Deferred account:** none.

### Stage 2 — Interfaces + local adapters + FastAPI skeleton — _account-free_
- **Goal:** the three ABCs, working local substitutes, and the web server with `/health`.
- **Files:** `app/email/base.py` (EmailClient ABC) + `app/email/file.py` (FileEmailClient→`./outbox/`),
  `app/llm/base.py` (LLM ABC), `app/store/base.py` (Store ABC) + `app/store/memory_store.py`
  (InMemoryStore — real dicts, atomic claim), `app/queue/tasks.py` (inline dispatch; tasks path
  stubbed), `app/main.py` (`/health`; `/inbound` + `/process` wired to inline, parse via injected
  client — fixture-driven until AgentMail).
- **Validation:** `GET /health` → 200 (TestClient); InMemoryStore `claim_message` returns
  True-once-then-False (idempotency), `upsert_thread`/`get_thread` round-trip; FileEmailClient
  writes an `.eml`-style file (+ copies attachment) to `./outbox/`.
- **Parallel subagents:** email ‖ store ‖ main/queue ‖ tests.
- **Deferred account:** none.

### Stage 3 — Scraper: navigate / search / metadata text / counts — _needs gov-site egress_
- **Goal:** lift the spike's validated locators/timing for everything up to (not incl.) downloads.
- **Files:** `app/scrape/browser.py` (`launch_browser` factory — headless flag, slow_mo, UA,
  accept_downloads, default timeout), `app/scrape/selectors.py` (URL, placeholder, DOC_TYPES,
  timeouts), `app/scrape/scraper.py` (`goto_and_ready`, `search`, `read_counts` via regex over
  `body.innerText`, raw results-text accessor for later LLM metadata extraction).
- **Validation (live M12205, §12 oracle):** assert **structure + live-read**, never the drifting
  literals — app paints (`.fm-textarea` present, loader hidden, never `networkidle`); search returns
  found=True; all five `"<Type> - <N>"` counts parse as ints; title line with `" - $"`, an amount,
  two `MM/DD/YYYY` dates, Type and Category strings all present in live text. One matter, polite pacing.
- **If validation fails:** first check whether the gov site is unreachable (blocked egress looks
  like broken scraper code). If unreachable → STOP, report the network gate; do not thrash.
- **Deferred account:** none (uses live site, not a credential).

### Stage 4 — Scraper: download loop + mandatory guards — _needs gov-site egress_
- **Goal:** the two-step download, virtualized scroll-collect-dedupe, both guards, per-doc retry.
- **Files:** `app/scrape/scraper.py` (`download_type(document_type, limit)`; `dismiss_modal`
  empty-tab guard run before every tab/download action; `scroll_doc_list`; `open_download_modal`;
  PDF-Only filter; per-document retry-then-skip wrapped in the §9 taxonomy; `DownloadedDoc`
  assembly; `read_metadata_and_counts` returns `MatterMetadata` — counts regex now + LLM metadata
  parse wired in Stage 5/6). Returns `ScrapeResult`.
- **Validation (live M12205, §12):** 10-document pull from "Other Documents" → 10 distinct docs,
  0 duplicates, all valid files (magic-byte check); empty tab (Transcripts/Recordings) returns `[]`
  and the curtain is dismissed; a simulated per-doc download failure is retried then skipped
  (counted in `failed`) without ending the loop.
- **Deferred account:** none.

### Stage 5 — LLM: classify / extract / summarize / metadata — _needs Anthropic (have it)_
- **Goal:** the LLM layer behind the ABC, numbers-authoritative summaries.
- **Files:** `app/llm/prompts.py`, `app/llm/anthropic_client.py` (AnthropicLLM: `classify` Haiku;
  `extract` Sonnet structured output incl. fuzzy doc-type mapping + nulls; `summarize` Sonnet with
  post-gen assertion that matter# and "{k} of {N}" survive, else deterministic §5 template
  fallback; `extract_metadata(text)→MatterMetadata` Sonnet for the Stage-3/4 results text).
- **Validation:** unit tests on crafted emails — classify → request|conversational|junk; extract →
  matter/type incl. fuzzy ("other docs"→"Other Documents") and missing-field nulls; summarize keeps
  exact numbers and falls back to template when the model drifts (numbers never model-invented).
  LLM calls live against Anthropic; assert on structure/invariants.
- **Deferred account:** Anthropic (present).

### Stage 6 — Pipeline inline, happy path — _needs Anthropic + gov-site egress_
- **Goal:** wire `process_job` end-to-end (§4) through `FileEmailClient`.
- **Files:** `app/pipeline.py` (`process_job`: classify→extract(+validate)→scrape→package→summarize
  →send→mark done; correlation-id bound; exactly one outbound action), `app/main.py` `/inbound`
  inline path, `app/package/packager.py` minimal (attach branch only here; link branch in Stage 7).
- **Validation (§12 e2e):** an `InboundEmail` fixture ("M12205, Other Documents") through
  `process_job` with `FileEmailClient` produces the correct reply body + ZIP attachment in
  `./outbox/`; clarification fixtures (missing matter / missing type / both) produce the §5 copy;
  empty-type and matter-not-found fixtures produce their replies.
- **Deferred account:** Anthropic (present) + live site.

### Stage 7 — Packager + robustness — _needs Anthropic_
- **Goal:** streaming zip + threshold branch + GCS-link fallback (stubbed), retries, idempotency,
  failure email, JSON logging, trace-on-failure, Sentry.
- **Files:** `app/package/packager.py` (incremental on-disk zip, delete-source-as-you-go,
  threshold gate, GCS upload + V4 signed URL — GCS path stubbed locally, name de-collision),
  `app/pipeline.py` + `app/errors.py` (tenacity backoff on RetryableError; idempotency via
  InMemoryStore claim; terminal failure email with job_id ref), `app/observability.py` (JSON logs,
  lifecycle lines, trace/screenshot/page.content on scrape failure).
- **Validation:** unit tests — threshold attach-vs-link branch (GCS stubbed), zip integrity/round-
  trip, retryable-vs-terminal classification, duplicate `message_id` skipped (idempotency), terminal
  error yields failure email with reference id.
- **Deferred account:** Anthropic (present); GCS stubbed.

### Stage 8 — Thread-context follow-up feature — _needs Anthropic_
- **Goal:** the differentiator — inherit-on-null, explicit-overrides-inherited, persisted in-memory.
- **Files:** `app/pipeline.py` (merge `get_thread` during extract; set `inherited_*` flags;
  `upsert_thread` after successful scrape), `app/store/memory_store.py` (already backs it).
- **Validation:** follow-up email omitting the matter inherits the thread's last matter/type;
  an explicit value in the follow-up overrides the inherited one; flags logged. Tested fully with
  no database.
- **Deferred account:** Anthropic (present).

### Stage 9 — README (decision log) + conversational-ack flex — _account-free_
- **Goal:** the final-deliverable README (ADR-style decision log per §13) + the cut-order
  conversational ack; finalize everything buildable without external accounts.
- **Files:** `README.md` (onboarding, repo walkthrough, decision log covering every §13 decision,
  failure taxonomy, future-improvements), `app/llm`/`app/pipeline` conversational one-line ack.
- **Validation:** README review against §13 checklist; ack fires only on clear thanks/greeting.
- **Deferred account:** none. _(Deploy section of README revisited after Stage 12.)_

--- HARD GATES BELOW: each stage requests its specific deferred credential, then continues ---

### Stage 10 — SupabaseStore adapter + provisioning — **PAUSE: needs `SUPABASE_URL`/`SUPABASE_KEY`**
- **Goal:** prod store as a pure adapter swap; zero feature-logic change.
- **Files:** `app/store/supabase_store.py` (same `Store` interface; `claim_message` =
  `INSERT … ON CONFLICT DO NOTHING RETURNING`; thread upsert), apply `schema.sql`, flip via `ENV`.
- **Validation:** same Store test-suite passes against Supabase; idempotency gate verified live;
  thread upsert/get round-trips.

### Stage 11 — AgentMailClient (real inbound/outbound) — **PAUSE: needs `AGENTMAIL_*`**
- **Goal:** bind AgentMail against its **live docs** (do not guess the API).
- **Files:** `app/email/agentmail.py` (inbound webhook payload → `InboundEmail`; `send_reply`
  in-thread + base64 attachment; signature verification in `/inbound`). Read
  `https://docs.agentmail.to/llms-full.txt` / SDK first.
- **Validation:** signature-verify rejects bad payloads (401); a live send + inbound round-trip;
  oversized-send try/except falls back to the GCS link.

### Stage 12 — Cloud Tasks + /process OIDC + Dockerfile + deploy — **PAUSE: needs GCP/GCS**
- **Goal:** `QUEUE_MODE=tasks`, OIDC-verified `/process`, container, Cloud Run + queue + bucket.
- **Files:** `app/queue/tasks.py` (Cloud Tasks enqueue + OIDC), `app/main.py` `/process` OIDC verify,
  `Dockerfile`, deploy config. Wire real GCS upload + signed URL (SA token-creator self-sign).
- **Validation:** live email → Cloud Task → `/process` → reply; `5xx` only on RetryableError;
  oversized zip delivers a working signed link. Finalize README deploy section.

---

## Progress
- [x] Stage 1 — Scaffold core (commit 6811458; 39 tests; review: no blocking)
- [x] Stage 2 — Interfaces + local adapters + FastAPI (61 tests; review: no blocking)
- [ ] Stage 3 — Scraper: navigate/search/metadata/counts (live)
- [ ] Stage 4 — Scraper: download loop + guards (live)
- [ ] Stage 5 — LLM
- [ ] Stage 6 — Pipeline inline happy path
- [ ] Stage 7 — Packager + robustness
- [ ] Stage 8 — Thread-context follow-up
- [ ] Stage 9 — README + conversational ack
- [ ] Stage 10 — SupabaseStore (PAUSE: Supabase creds)
- [ ] Stage 11 — AgentMailClient (PAUSE: AgentMail creds)
- [ ] Stage 12 — Cloud Tasks + deploy (PAUSE: GCP)
