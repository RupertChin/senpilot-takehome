# Senpilot Regulatory Email Agent

An email agent for the **Nova Scotia UARB** public-documents portal. A user emails a **matter
number** (e.g. `M12205`) and a **document type** (Exhibits / Key Documents / Other Documents /
Transcripts / Recordings); the agent scrapes the UARB FileMaker WebDirect portal, downloads up to
10 documents of that type, zips them, and replies in-thread with the ZIP (or a download link if
it's too large) plus a plain-language metadata summary.

It also remembers context per email thread: a follow-up that omits the matter number ("ok, then
the Exhibits") inherits it from the previous request in that thread.

---

## Status

Built and validated **locally, end to end**, against live infrastructure:

- ✅ Scraper validated against the live UARB portal (matter **M12205**): search → results →
  metadata → all five counts → multi-document download (10 distinct docs, 0 duplicates, valid
  files) → empty-tab guard.
- ✅ LLM layer (classify / extract / summarize / metadata) validated against the live Anthropic API.
- ✅ Full pipeline validated end to end: an `InboundEmail` fixture through `process_job` produces
  the correct reply **and ZIP attachment** in `./outbox/` (the local email substitute), job marked
  done, correlation-id bound throughout.
- ✅ Thread-context follow-up, robustness (retry taxonomy, failure email, trace-on-failure,
  streaming zip + oversized-link branch), and the conversational ack — all built and tested.

**Deferred to deploy** (each is a pure adapter swap or infra wiring, not feature work):

| Deferred | Substitute used locally | Lands in |
|---|---|---|
| Supabase (state store) | `InMemoryStore` (dicts) | the `SupabaseStore` swap |
| AgentMail (real inbound/outbound) | `FileEmailClient` → `./outbox/` | the AgentMail binding |
| Cloud Tasks / Cloud Run / GCS | `QUEUE_MODE=inline`, `LocalStubUploader` | the deploy stage |

The whole system — thread-reply included — runs on **only an Anthropic API key** plus network
egress to the gov site. See [Running it](#running-it).

---

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env          # fill in ANTHROPIC_API_KEY (the only secret needed locally)
uvicorn app.main:app --reload # serves /health, /inbound, /process
```

`.env` holds secrets and is git-ignored — never commit it. `.env.example` lists every variable,
grouped by when you need it (local-now vs. deploy-time).

### Running it

- **Local dev (default):** `ENV=local`, `QUEUE_MODE=inline`. The app uses `InMemoryStore`,
  `FileEmailClient` (replies land in `./outbox/`), runs the browser headed with `slow_mo`, and
  processes each inbound email inline (no queue). Only `ANTHROPIC_API_KEY` is required.
- **Prod:** `ENV=prod` (auto-detected on Cloud Run via the injected `K_SERVICE`), `QUEUE_MODE=tasks`.
  Swaps in `SupabaseStore`, `AgentMailClient`, headless Chromium, Cloud Tasks dispatch, and the real
  GCS uploader. Configured entirely through env vars (see `app/config.py`).

The environment switch is the single source of truth — `Settings` in `app/config.py` derives every
local-vs-prod behavior (browser headless, log format, email client, store, queue mode, trace policy)
from `ENV`.

### Testing

```bash
pytest                       # the default suite — offline, no network, fast
pytest -m live               # live UARB scraper + end-to-end (hits the gov site; polite, one matter)
pytest -m llm                # live Anthropic LLM tests (cost a few API calls)
pytest -m supabase           # live SupabaseStore contract (needs SUPABASE_URL/SUPABASE_KEY)
pytest -m agentmail          # live AgentMail send/reply + signature (needs AGENTMAIL_*)
```

Offline tests are the CI default (deterministic, no network). The live markers are opt-in because
they hit external services. The deterministic core — count/metadata regex, the
scroll-collect-dedupe + retry orchestration, the LLM validation/fallback logic, every pipeline
branch, the packager, and the thread feature — is fully covered **offline**, so a regression is
caught without touching the gov site or the API.

### Driving a request locally

With `ENV=local` (the default), boot the server and POST an inbound email to `/inbound`. Locally
there's **no webhook signature required** (the `FileEmailClient` verify is a no-op) and it accepts
simple field aliases:

```bash
uvicorn app.main:app --reload          # serves on http://localhost:8000

curl -X POST http://localhost:8000/inbound -H 'content-type: application/json' \
  -d '{"message_id":"local-1","from":"you@example.com","to":"agent@local",
       "subject":"docs","body_text":"Please send the Other Documents for M12205"}'
```

What happens: a **headed** Chromium window opens (local runs headed with `slow_mo` so you can
watch), the agent scrapes M12205 live, and the reply lands in **`./outbox/`** as an `.eml` file with
the ZIP beside it. Processing is **inline**, so the HTTP response returns only after the scrape
finishes (~30–60 s for 10 docs; lower `MAX_DOCUMENTS` in `.env` to speed it up). The same flow is
covered by `pytest -m live tests/test_pipeline_live.py`.

> In `ENV=local` the email client is `FileEmailClient` — replies go to `./outbox/`, **not** through
> AgentMail, and the store is in-memory (nothing persisted). This is the clean way to exercise the
> scrape/parse/summarize logic with no side effects. To use a **real AgentMail inbox** locally
> instead, see the local-dev note under [Future improvements](#future-improvements).

---

## How it works

```
AgentMail → POST /inbound
  ├─ verify webhook signature                         (reject 401 if bad)
  ├─ parse → InboundEmail
  ├─ idempotency: claim_message(message_id)           (INSERT … ON CONFLICT DO NOTHING)
  │     already seen → 200, skip
  ├─ QUEUE_MODE=tasks  → enqueue Cloud Task → 200
  │  QUEUE_MODE=inline → await process_job → 200
  └─ (tasks) Cloud Tasks → POST /process (OIDC) → process_job

process_job(message_id):
  classify (Haiku) → request | conversational | junk
    junk → done, no reply;   conversational → one-line ack → done
  extract (Sonnet) → {matter_number?, document_type?}
    merge thread-context (inherit a null field; explicit always wins)
    missing required field → clarification reply → done
  scrape(matter, type)
    not found → clarification → done;   0 of that type → "zero" reply (+ counts) → done
    all downloads failed → retryable infra failure
  package(docs) → streaming zip; ≤ threshold attach, else GCS link
  summarize (Sonnet, exact numbers) → reply (attachment, or link + size reason)
  upsert thread-context;  mark done
  on infra failure (inline) → failure email w/ reference id → mark failed
```

### Repo structure

```
app/
  main.py            FastAPI: /inbound, /process, /health; idempotency claim + dispatch
  config.py          Settings (pydantic-settings); ENV/QUEUE_MODE auto-detect; all env-driven behavior
  models.py          all Pydantic schemas + matter/enum validation
  pipeline.py        process_job() — the orchestration + error-taxonomy + thread-context merge
  replies.py         deterministic §5 user-facing copy (clarification/not-found/empty/failure/ack)
  errors.py          RetryableError / TerminalError + classify_exception
  observability.py   structlog (JSON prod / console local), correlation-id contextvar, Sentry init
  deps.py            factories: pick InMemoryStore/FileEmailClient/AnthropicLLM by ENV
  email/             EmailClient ABC; FileEmailClient (→ ./outbox/); AgentMailClient (deploy)
  llm/               LLM ABC; AnthropicLLM (forced-tool structured output); prompts + §5 renderer
  scrape/            browser factory; content-anchored selectors; UARBScraper + scrape_matter
  package/           streaming zip packager; Uploader (LocalStub now, GCS at deploy)
  store/             Store ABC; InMemoryStore (local); SupabaseStore (deploy)
  queue/             inline dispatch now; Cloud Tasks at deploy
tests/               offline suite + `live` (gov site) and `llm` (Anthropic) opt-in markers
reference/           the validated scraper spike (spike.py) + findings — the scraper's source of truth
schema.sql           Supabase tables (jobs + threads) for the deploy store swap
```

The module boundaries are deliberate: browser code lives only in `scrape/`, AgentMail only in
`email/agentmail.py`, GCP only in `package/`/`store/`/`queue/`. The scraper depends on no LLM (the
metadata extractor is injected as a callable), so the layers stay swappable.

---

## Decision log (ADR-style)

Every load-bearing decision, with its rationale and the alternatives rejected.

### Transport — an email agent over a webhook (AgentMail)
The product is an email agent, so inbound is a provider webhook (`POST /inbound`) and outbound is an
in-thread reply. AgentMail gives the agent its own inbox and a webhook with signature verification.
Its exact API (webhook payload shape, send-with-attachment) is bound against its live docs in
`email/agentmail.py` — niche enough that training priors are unreliable, so it's read, not guessed.
*Rejected:* polling a mailbox over IMAP/Gmail API (no native per-agent inbox, polling latency, more
state to manage than a push webhook).

### Host — Cloud Run
A request-driven container fits a webhook workload, scales to zero, and ships the Playwright base
image cleanly. *Rejected:* always-on VM (idle cost, manual scaling); Cloud Functions (Playwright +
Chromium is awkward in the function runtime).

### Queue — Cloud Tasks, with an inline fallback
Scraping + downloads can take tens of seconds; doing that inside the webhook request risks provider
timeouts and lost work on a crash. Cloud Tasks decouples ingestion from processing and gives free
retries with backoff. `QUEUE_MODE=inline` runs `process_job` directly in the request — used for
local dev and a first deploy. *Rejected:* Celery/Redis/SQS (heavier ops for one queue); processing
inside the webhook (timeout + at-most-once delivery risk).

### Scaling — concurrency 1, max-instances 3, tasks dispatch = 3
Each job drives a full Chromium + a growing on-disk zip (a single Exhibit was 48 MB), so peak memory
is real. Request concurrency = 1 keeps one heavy job per instance; max-instances and the Cloud Tasks
`max_concurrent_dispatches` are pinned equal (3) so the queue never outruns the runtime. Polite to
the gov site as a side effect.

### Browser — Playwright (Chromium), behind a one-function swap
The portal is 100% client-rendered FileMaker WebDirect on Vaadin; a real browser is mandatory (no
HTML form to post). Playwright drives it headless in prod. `scrape/browser.py` is the **only** place
that names the engine, so a future camoufox/stealth swap is one function. *Rejected:* requests/httpx
(nothing to scrape server-side); Selenium (Playwright's auto-waiting + tracing are better here).

### Scraper mechanics — lifted verbatim from a validated spike
The portal has sharp edges (see `reference/FINDINGS.md`): no `<input>` elements (fields are
`div.fm-textarea` you click-then-type, never `fill()`); five ambiguous "Search" buttons
disambiguated by geometry; counts embedded in tab labels (`"Exhibits - 13"`); a **two-step modal**
download (Go Get It → "Download Files" modal → per-file button → `expect_download`); a blocking
**"No Matching Records" modal** on empty tabs whose curtain wedges every later click; a
**virtualized** document list (~8 rows in the DOM). All of this is transcribed from a spike that was
confirmed working live, not re-derived. `networkidle` is never used (Atmosphere push never idles) —
readiness is content-polling.

### Anti-bot posture — polite, not adversarial (yet)
A realistic desktop UA, a single session, sequential downloads, and a `POLITE_DELAY_S` courtesy gap.
The gov site has no `robots.txt` and no detection observed in the spike, so no stealth is needed
today. The browser factory is the swap point for an escalation ladder (stealth → camoufox →
residential proxies) **if** detection ever appears — deliberately not built now (YAGNI).

### Agentic framing — LLMs as typed components, not an autonomous agent
The flow is a deterministic pipeline; the LLM does three bounded jobs (classify, extract, compose a
summary) behind an `LLM` interface, with **structured/forced-tool output** so responses are schema-
constrained. There is no open-ended tool-using agent loop: the task is well-specified, the cost of a
wrong action is high, and a fixed pipeline is far easier to test and reason about. The model has **no
action tools** — it cannot fetch, send, or download — so prompt injection from email or page content
can at worst flip a label or a field, never trigger a side effect.

### LLM metadata, deterministic counts
Document **counts** are read by regex over the results screen and are **authoritative** — never
produced by the model. The descriptive **metadata** (organization, project, amount, type, category,
dates) is parsed by the LLM from the results text, because it's fuzzy free-text (the amount rides
inside the title line; dates are `MM/DD/YYYY`; Type and Category are distinct fields). The success
summary is LLM-composed but **validated**: it must reproduce the matter number, the "{k} of {N}"
figures, and every per-type count verbatim, or the agent falls back to a deterministic template.
Numbers shown to the user are always exact.

### "Document" = one grid row
The ≤10 limit counts **documents (rows)**, matching the site's tab counts; each selected row
contributes **all** the file-buttons in its download modal to the zip. Multi-file rows weren't
observed in M12205 (every modal had one file) — this is a defensive rule inferred from the modal's
plural wording, and it degenerates to one-file-per-row if multi-file rows don't exist.

### State store — Supabase (hosted Postgres), `InMemoryStore` locally
One table is the idempotency gate **and** the job record (a one-statement
`INSERT … ON CONFLICT DO NOTHING RETURNING`), and a second holds thread context — both behind one
`Store` interface, so idempotency and the thread feature are built and fully tested **locally with no
database**, and Supabase is a pure adapter swap before deploy. *Rejected:* Firestore (GCP-native, no
extra vendor — but less familiar and needs a local emulator); SQLite (least setup, but doesn't
survive Cloud Run cold starts, which would make thread-follow-up flaky).

*Access posture:* `jobs` and `threads` are **server-only** tables — no browser/public client ever
touches them. Prod should connect with the **service-role key** (bypasses RLS, stays in server
env). The MCP-provisioned dev project uses the publishable/anon key, so its migration disables RLS
and grants the API roles directly; the Supabase advisor flags RLS-disabled as critical, which is
acceptable here **only** because the key never leaves the server. `jobs.inbound` stores the full
inbound email (sender/subject/body) as the job record — server-side, but a retention policy and the
service-role posture are the right call for any real deployment.

### Thread follow-up — inherit-on-null, explicit-overrides-inherited
A follow-up in the same thread that omits the matter (or type) inherits it from the thread's last
request; an explicit value in the new email always wins. Context is keyed by the provider's thread
id and upserted after any successful scrape (including an empty-type result, since the matter is
still valid). Inherited values are re-validated, so nothing unvalidated can enter the scraper via
thread context. This is the differentiator and it required no database to build or test.

### Delivery — base64-aware size **threshold** as the primary gate
The reply attaches the ZIP when the **raw** zip ≤ `ATTACH_THRESHOLD_BYTES` (default **18 MB**); above
that it uploads to GCS and replies with a V4 signed link **stating the size reason**. The threshold
sits below 25 MB on purpose: attachments are sent base64 (~33% larger on the wire), so 18 MB raw
keeps the encoded message comfortably under common 25 MB limits.

*Why threshold-first, not try-attach-then-fall-back:* AgentMail reports oversize failures
**asynchronously** via bounce webhooks — a successful `send()` means accepted-for-processing, not
delivered — so you cannot rely on `send()` throwing. Worse, AgentMail penalizes bounces hard
(permanent block of the bounced recipient + an account-level bounce-rate ceiling), so letting
oversized mail bounce as the signal would risk blocking the very user being served. The deterministic
threshold is therefore the primary gate. *Secondary net:* the send is still wrapped so a synchronous
oversize rejection falls back to the link — cheap insurance, not depended upon. *Rejected:*
bounce-as-signal (too late + reputation-penalized); always-link (diverges from "attach the ZIP" for
the common small case); multi-zip-split (ugly, burns the send-rate cap, some servers strip zips).

### Error taxonomy — Retryable vs Terminal
`RetryableError` (timeouts, 5xx, connection resets, a download that didn't start, transient LLM
errors) is retried with backoff; unresolved at `/process` it returns 5xx so Cloud Tasks retries.
`TerminalError` (matter-not-found, empty type, invalid request) gets a friendly user reply and a 200
— never retried. Per-document download failures are isolated: retry, then skip-and-report (counted
in `ScrapeResult.failed`), never failing the whole job for one bad document. Only infrastructure
failures produce the "something went wrong, reference {job_id}" email.

---

## Failure taxonomy (diagnosing a bad run)

| Symptom | Likely cause | Response |
|---|---|---|
| Search returns, but no `"<Type> - <N>"` appears | DOM/selector rot (FileMaker layout changed) | `ScrapeStructureError` (retryable) → alert via Sentry |
| A click hangs the full action timeout | an undismissed modal (empty-tab guard regression) | retry; investigate the `dismiss_modal` guard |
| Clean "matter not found" | legitimate user error | clarification reply, no alert |
| `downloaded == 0` with `type_count > 0` | all per-doc downloads failed (transient/infra) | retryable → failure email (inline) / Cloud Tasks retry (prod) |
| Summary missing a count or the link | model drift | auto-fallback to the deterministic §5 template (numbers stay exact) |
| Oversized zip | matter with large files | GCS link branch (states the size reason) |

On any scrape failure a Playwright **trace + screenshot + page content** are saved for triage
(always locally; on-failure-only in prod).

---

## Future improvements

Out of scope for this build, documented here:

- A heavier durable queue (Celery/SQS) with a dead-letter queue and load-shedding.
- A warm browser pool / browser-as-a-service for high volume.
- An anti-detection escalation ladder (stealth → camoufox → residential proxies), wired at the
  `scrape/browser.py` swap point.
- A scheduled selector-rot canary against M12205.
- Multi-matter / batch requests; per-sender rate limiting.
- AgentMail `message.bounced` / `message.rejected` / `message.delivered` webhook handling for
  delivery observability and a last-resort "send the link as a follow-up" recovery.
- A failure email on exhausted Cloud-Tasks retries (today the failure email is the inline-mode path;
  prod relies on Sentry/DLQ).
- A retention sweep for saved scrape traces (`page.html` accumulates under local `trace_always`).
- **Local-dev: connect a real AgentMail inbox without a public URL.** Today, driving a request
  locally means POSTing to `/inbound` with `curl` (`ENV=local`, replies → `./outbox/`). To exercise
  a *real inbox* end-to-end locally there are two paths, neither yet wired: (a) an **AgentMail
  websocket listener** — a small `python -m app.listen` that subscribes to the inbox over AgentMail's
  websocket and feeds `message.received` events into `process_job`, so emailing the inbox works with
  **no public URL / no ngrok** (AgentMail's recommended local-dev approach); or (b) **ngrok** —
  expose `localhost:8000`, register the tunnel URL as the AgentMail webhook, and the real signed
  `/inbound` path fires. The websocket listener is the cleaner option and the natural next addition.
- **Scraper: fail-fast on a stuck modal at the virtualized-scroll boundary.** A row whose
  Go-Get-It modal click hangs currently eats the full 20 s default action timeout before the loop
  skips it (output stays correct — it's a latency wrinkle, not a failure). A short explicit
  modal-open timeout plus a `dismiss_modal()` guard before each open would turn that ~20 s stall into
  a couple-second skip.
- The downstream search index (the actual Regulatory Agent this feeds).

---

## Reference

`reference/spike.py` and `reference/FINDINGS.md` are the validated scraper discovery — the source of
truth for the portal's selectors, timing, and gotchas. The scraper lifts them rather than
re-deriving against FileMaker's quirks. `IMPLEMENTATION_SPEC.md` is the authoritative build spec.
