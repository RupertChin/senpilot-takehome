# Senpilot Regulatory Email Agent — Implementation Spec

**Audience:** an autonomous coding agent (and its harness) that will plan and implement this
system. This document is self-contained: it carries all context needed to build, test, and deploy.
Favor the specifics here over re-deriving them. Where a value is given, use it; where an interface
is given, implement exactly it.

**What it builds:** an email agent. A user emails a matter number + document type; the agent
scrapes the Nova Scotia UARB FileMaker WebDirect portal, downloads up to 10 documents of that
type, zips them, and emails the user back with the zip (or a link if large) plus a metadata
summary. Robustness, observability, and a clean deploy are first-class requirements.

---

## 0. Decisions to confirm with the human before/while implementing

These were chosen to remove ambiguity. They are provisionally fixed; if the human overrides one,
adjust the affected module only.

1. **State store = Supabase (hosted Postgres) in prod; `InMemoryStore` (dicts) locally.** Chosen
   for lowest setup friction given existing fluency: one table covers idempotency + jobs +
   thread-context, and the idempotency gate is a one-line `INSERT ... ON CONFLICT DO NOTHING
   RETURNING`. Both back the same `Store` interface, so idempotency and the thread-follow-up feature
   are built and tested locally with **no database**, and Supabase is a pure adapter swap before
   deploy (build order §11). *Alternatives considered and rejected:* Firestore (GCP-native, no
   separate vendor — but less familiar and an emulator for local); SQLite (least setup, but does not
   survive Cloud Run cold starts, which would make thread-follow-up flaky). Record in the README
   decision log.
2. **"Document" = one grid row.** Each selected document contributes **all** file-buttons in its
   "Download Files" modal to the zip. The **≤10 limit counts documents (rows)**, not files. The
   summary reports document (row) counts, matching the site's tab counts.
   *Note:* multi-file rows were **not observed** in M12205 testing (every modal had exactly one
   file) — this rule is a free safety net inferred from the modal's plural wording ("download each
   file"); it degenerates to one-file-per-row if multi-file rows don't exist. Record as a
   defensive (not observed-required) choice in the README.
3. **LLM = Anthropic Claude:** `claude-haiku-4-5` for classification, `claude-sonnet-4-6` for
   extraction and summary composition. Behind the `LLM` interface (§7.5) so swappable.

Everything below is fully specified. The only deliberately-unbound surface is **AgentMail's exact
API** (webhook payload field names, send-with-attachment call): implement `AgentMailClient` against
AgentMail's current documentation, mapping it onto the `EmailClient` interface (§7.3). Do not
invent AgentMail endpoint shapes — read their docs.

---

## 1. Tech stack & dependencies

- **Python 3.11**, async throughout.
- **FastAPI** + **uvicorn** — webhook server (`/inbound`, `/process`, `/health`).
- **Playwright** (async, Chromium) — the scraper. Base Docker image
  `mcr.microsoft.com/playwright/python:v1.48.0-jammy` (or current) ships Chromium + system deps.
- **pydantic v2** + **pydantic-settings** — schemas and config.
- **tenacity** — retry/backoff.
- **structlog** — structured JSON logging.
- **google-cloud-tasks**, **google-cloud-storage** — queue and large-file storage.
- **supabase** (or **asyncpg**/**psycopg** against the Postgres connection string) — state store.
- **anthropic** — LLM calls.
- **sentry-sdk** — exception alerting.
- AgentMail access via its **official SDK if available, else `httpx`** against its REST API.

Pin versions in `requirements.txt`. No other heavyweight deps.

---

## 2. Repo structure

```
app/
  main.py                 # FastAPI app: /inbound, /process, /health
  config.py               # Settings (pydantic-settings); ENV + QUEUE_MODE
  models.py               # all Pydantic schemas (§6)
  pipeline.py             # process_job(): orchestration + error-taxonomy mapping (§7.8)
  errors.py               # RetryableError / TerminalError + classify_exception (§9)
  observability.py        # structlog config, correlation-id contextvar, sentry init
  email/
    base.py               # EmailClient ABC + InboundEmail/OutboundEmail (§7.3)
    agentmail.py          # AgentMailClient — bind to AgentMail API
    file.py               # FileEmailClient — writes reply (+ attachment) to ./outbox/ for local dev
  llm/
    base.py               # LLM ABC: classify(), extract(), summarize()
    anthropic_client.py   # AnthropicLLM
    prompts.py            # prompt templates (§7.4)
  scrape/
    browser.py            # launch_browser(settings) -> (browser, context) factory (§7.6)
    selectors.py          # all content-anchored locators + timeouts (§7.6, from spike)
    scraper.py            # UARBScraper: search/read/download (§7.6)
  package/
    packager.py           # stream downloads -> on-disk zip; size check; GCS upload + signed URL (§7.7)
  store/
    base.py               # Store ABC: idempotency + thread context (§7.9)
    memory_store.py       # InMemoryStore — local default (dicts); backs both store uses
    supabase_store.py     # SupabaseStore — prod impl (Postgres); same interface, pure swap
  queue/
    tasks.py              # enqueue() via Cloud Tasks; inline dispatch (§7.10)
Dockerfile
requirements.txt
.env.example
README.md                 # onboarding + design rationale + decision log (produced last; see §13)
tests/
  test_validation.py      # matter/doc-type parsing + clarification
  test_packager.py        # size threshold branch, zip integrity
  test_scraper_smoke.py   # live M12205 smoke (marked slow/optional)
```

Module boundaries are normative — keep browser-specific code inside `scrape/`, AgentMail-specific
code inside `email/agentmail.py`, GCP-specific code inside `store/`, `package/`, `queue/`.

---

## 3. Configuration (`app/config.py`)

`Settings(BaseSettings)`, read from env. Auto-detect environment: `ENV = os.getenv("ENV") or
("prod" if os.getenv("K_SERVICE") else "local")` (`K_SERVICE` is injected by Cloud Run).

| Setting | Env var | Default | Notes |
|---|---|---|---|
| env | `ENV` | auto | `local` \| `prod` |
| queue_mode | `QUEUE_MODE` | `inline` (local) / `tasks` (prod) | `inline` \| `tasks` |
| anthropic_api_key | `ANTHROPIC_API_KEY` | — | required |
| agentmail_api_key | `AGENTMAIL_API_KEY` | — | required |
| agentmail_inbox | `AGENTMAIL_INBOX` | — | the agent's address |
| agentmail_webhook_secret | `AGENTMAIL_WEBHOOK_SECRET` | — | for signature verification |
| supabase_url | `SUPABASE_URL` | — | required (or use `DATABASE_URL`) |
| supabase_key | `SUPABASE_KEY` | — | service-role key (server-side) |
| gcp_project | `GCP_PROJECT` | — | required in prod |
| gcs_bucket | `GCS_BUCKET` | — | used only when the zip exceeds the attach threshold |
| tasks_queue | `TASKS_QUEUE` | — | Cloud Tasks queue id |
| tasks_location | `TASKS_LOCATION` | `us-central1` | |
| process_url | `PROCESS_URL` | — | absolute URL of `/process` for Cloud Tasks target |
| tasks_invoker_sa | `TASKS_INVOKER_SA` | — | service account email for OIDC on the task |
| sentry_dsn | `SENTRY_DSN` | "" | empty disables |
| classify_model | `CLASSIFY_MODEL` | `claude-haiku-4-5` | |
| extract_model | `EXTRACT_MODEL` | `claude-sonnet-4-6` | |
| summary_model | `SUMMARY_MODEL` | `claude-sonnet-4-6` | |
| max_documents | `MAX_DOCUMENTS` | `10` | hard cap per request |
| attach_threshold_bytes | `ATTACH_THRESHOLD_BYTES` | `18_000_000` | **raw** zip ≤ → attach; > → GCS link. Below 25MB to absorb base64 inflation (§7.7). AgentMail's own ceiling is undocumented, so this threshold is the gate (not a runtime probe). |
| signed_url_ttl_hours | `SIGNED_URL_TTL_HOURS` | `72` | TTL of the link used for oversized zips |
| polite_delay_s | `POLITE_DELAY_S` | `0.6` | gap between downloads |
| max_instances | (deploy flag) | `3` | Cloud Run |
| tasks_max_dispatch | (queue config) | `3` | must equal max_instances |

**Environment-driven behavior** (single source of truth in `Settings`):

| Concern | local | prod |
|---|---|---|
| browser headless | `False` (slow_mo 250) | `True` |
| logs | pretty console | JSON to stdout |
| Playwright trace | always | on failure only |
| email client | `FileEmailClient` (→ `./outbox/`) | `AgentMailClient` |
| store | `InMemoryStore` (dicts) | `SupabaseStore` (Postgres) |
| queue_mode default | `inline` | `tasks` |
| sentry | off | on (if DSN) |

---

## 4. End-to-end flow

```
AgentMail → POST /inbound
  ├─ verify webhook signature                         (reject 401 if bad)
  ├─ parse → InboundEmail
  ├─ idempotency: Supabase INSERT ... ON CONFLICT DO NOTHING (message_id)
  │     conflict (already seen) → return 200 (skip)
  ├─ QUEUE_MODE=tasks  → enqueue Cloud Task(message_id) → return 200
  │  QUEUE_MODE=inline → await process_job(message_id) → return 200
  └─ (tasks) Cloud Tasks → POST /process (OIDC-verified) → process_job(message_id)

process_job(message_id):
  load InboundEmail (from the Supabase job record)
  ├─ classify (Haiku) → request | conversational | junk
  │     junk → mark done, no reply
  │     conversational → optional one-line ack (only if clearly a thanks/greeting) → done
  ├─ extract (Sonnet) → ParsedRequest {matter_number?, document_type?}
  │     merge thread-context (inherit matter_number/document_type when null)
  │     validate (regex + enum)
  │     missing/invalid required field → clarification reply → done
  ├─ scrape(matter_number, document_type) → ScrapeResult         [retry taxonomy §9]
  │     matter-not-found → clarification reply → done
  │     empty tab (count 0) → "zero of that type" reply (+ counts) → done
  ├─ package(downloaded docs) → zip on disk → if ≤ threshold: attach; else GCS upload + signed URL
  ├─ summarize (Sonnet, exact numbers, delivery, link?) → email body
  ├─ send success reply (attachment, or link + reason if too big; partial wording if k<N)
  ├─ upsert thread-context(thread_id, matter_number, document_type)
  └─ mark job done
  on TerminalError after retries → failure reply with reference id (= job_id) → mark failed
```

---

## 5. Email behaviors (exact)

All replies go in-thread (reply to the inbound message; preserve threading).

**Success** (LLM-composed, numbers deterministic — see §7.4):
> Hi, {matter} is about {org} - {project} - {amount}. It relates to {type} within the {category}
> category. The matter had an initial filing on {date_initial} and a final filing on {date_final}.
> I found {n_exhibits} Exhibits, {n_key} Key Documents, {n_other} Other Documents,
> {n_transcripts} Transcripts, and {n_recordings} Recordings. I downloaded {k} of the {N}
> {document_type}. **If attached:** "...and have attached them as a ZIP." **If linked (zip over
> the email size limit):** "...and have packaged them as a ZIP. It's {size}MB — too large to attach,
> so it's available here: {url} (link expires in {ttl}h)." The linking branch must state the reason
> (size over the attachment limit), not just drop a bare link.

**Partial success** (k < N requested, or some downloads failed): same as success, but phrase as
"I downloaded {k} of the {N} {document_type} ({failed} could not be retrieved and were skipped)".

**Clarification — missing matter number:** "I can fetch that, but I need a matter number (like
M12205). Which matter?"
**Clarification — missing/invalid doc type:** "Which document type would you like — Exhibits, Key
Documents, Other Documents, Transcripts, or Recordings?"
**Clarification — both missing:** combine the two.
**Matter not found:** "I couldn't find matter {matter} on the UARB portal — could you double-check
the number?"
**Empty type:** "Matter {matter} has 0 {document_type}. For reference it has {counts...}. Want a
different type?"
**Failure (terminal):** "Something went wrong fetching {matter}; please try again shortly.
Reference: {job_id}."

Clarification/empty/not-found are **terminal-but-friendly** (no retry). Only infrastructure/
transient failures produce the failure email.

---

## 6. Data models (`app/models.py`, Pydantic v2)

```python
DocumentType = Literal["Exhibits","Key Documents","Other Documents","Transcripts","Recordings"]

class InboundEmail(BaseModel):
    message_id: str            # provider message id (idempotency key)
    thread_id: str | None      # AgentMail thread id
    from_addr: str
    to_addr: str
    subject: str
    body_text: str             # plain-text body (strip quoted history if available)
    received_at: datetime

class ParsedRequest(BaseModel):
    matter_number: str | None  # validated ^M\d{4,6}$
    document_type: DocumentType | None
    inherited_matter: bool = False
    inherited_type: bool = False

class DocCounts(BaseModel):
    exhibits: int; key_documents: int; other_documents: int
    transcripts: int; recordings: int

class MatterMetadata(BaseModel):
    matter_number: str
    organization: str | None
    project: str | None
    amount: str | None         # e.g. "$69,275,000" (string; formatting preserved)
    type: str | None           # e.g. "Capital Expenditure Approvals"
    category: str | None       # e.g. "Water"
    status: str | None
    date_initial: str | None   # MM/DD/YYYY as shown
    date_final: str | None
    counts: DocCounts

class DownloadedDoc(BaseModel):
    doc_no: str
    filenames: list[str]       # 1+ files per document (row)
    paths: list[str]           # local temp paths
    total_bytes: int

class ScrapeResult(BaseModel):
    matter_number: str
    found: bool
    metadata: MatterMetadata | None
    requested_type: DocumentType
    type_count: int            # how many docs of the requested type exist
    documents: list[DownloadedDoc]
    requested: int             # = min(MAX_DOCUMENTS, type_count)
    downloaded: int            # len(documents) that succeeded
    failed: int

class JobRecord(BaseModel):
    job_id: str                # uuid4; also the user-facing reference id
    message_id: str
    status: Literal["processing","done","failed"]
    inbound: InboundEmail
    created_at: datetime
    updated_at: datetime

class ThreadContext(BaseModel):
    thread_id: str
    last_matter_number: str | None
    last_document_type: DocumentType | None
    updated_at: datetime
```

Validation rules: `matter_number` matches `^M\d{4,6}$` (uppercase; normalize input by
upper-casing and stripping spaces). `document_type` matched case-insensitively against the enum,
with light fuzzy mapping ("other docs" → "Other Documents") handled in extraction, not regex.

---

## 7. Component specs

### 7.1 Webhook ingestion (`app/main.py`)

- `POST /inbound`: verify AgentMail signature using `agentmail_webhook_secret` (reject `401` if
  invalid — signature failure is the one case that does **not** 200). Parse payload → `InboundEmail`
  (map AgentMail fields; consult their docs). Run idempotency check-and-set (§7.9). If new: persist
  `JobRecord(status=processing)`; then `inline` → `await process_job`; `tasks` → `enqueue`. Return
  `200` quickly in tasks mode; in inline mode return `200` after processing. Always `200` on
  duplicate.
- `POST /process`: only reachable by Cloud Tasks. Verify the OIDC token (audience = service URL).
  Body = `{message_id}`. Load `JobRecord`, call `process_job`. Return `200` on success, `5xx` on
  `RetryableError` (lets Cloud Tasks retry), `200` on `TerminalError` (already handled w/ a reply —
  do not let Tasks retry a user-error).
- `GET /health`: returns `200` (Cloud Run health check).

### 7.2 Pipeline (`app/pipeline.py`)
`async def process_job(message_id)` implements §4. Wrap each stage; bind a correlation id
(= job_id) into the structlog contextvar for the whole job. Map exceptions via `classify_exception`
(§9). Guarantee exactly one outbound action per job (a reply, an ack, or silent-drop) and a
terminal `status` write.

### 7.3 Email (`app/email/`)
```python
class EmailClient(ABC):
    async def parse_inbound(self, raw: dict) -> InboundEmail: ...
    async def send_reply(self, *, in_reply_to: InboundEmail, body: str,
                         attachment_path: str | None = None) -> None: ...
```
- `AgentMailClient`: implement against AgentMail's API. Inbound parsing maps their webhook payload
  (sender, recipient, subject, text body, message-id, thread-id, attachments) onto `InboundEmail`.
  `send_reply` sends in-thread with an optional attachment (base64). Honor the 100/day cap.
- `FileEmailClient` (local): `send_reply` writes `./outbox/{timestamp}_{to}.eml`-style file with the
  body and copies the attachment beside it. Used whenever `ENV=local`.

### 7.4 LLM (`app/llm/`)
```python
class LLM(ABC):
    async def classify(self, email: InboundEmail) -> Literal["request","conversational","junk"]: ...
    async def extract(self, email: InboundEmail) -> ParsedRequest: ...
    async def summarize(self, scrape: ScrapeResult, delivery: Literal["attach","link"],
                        link: str | None) -> str: ...
```
- **classify** (Haiku): cheap, permissive/high-recall. Output one label only (use a constrained/
  JSON response). "request" = anything that plausibly asks for documents; "conversational" =
  greeting/thanks/no actionable ask; "junk" = spam/automated/irrelevant.
- **extract** (Sonnet): structured output → `{matter_number, document_type}` (either may be null);
  do the fuzzy doc-type mapping here. Use Anthropic tool/JSON mode; validate against the schema.
- **summarize** (Sonnet): given the **exact** `ScrapeResult` numbers and `MatterMetadata`, compose
  the §5 success body. **Numbers are authoritative and must not be altered.** After generation,
  assert the output contains the correct matter number and the "{k} of {N}" figures; if the check
  fails, fall back to a deterministic template render of §5. Counts/k/N are never produced by the
  model from scratch — they're interpolated facts the model must preserve.
- `prompts.py` holds the templates; keep them readable and versioned.

### 7.5 — (reserved)

### 7.6 Scraper (`app/scrape/`) — confirmed against the live site for M12205 (headed + headless)

**`browser.py`**: `launch_browser(settings) -> (browser, context)`. Owns headless flag, `slow_mo`,
`user_agent` (a realistic desktop Chrome UA), `accept_downloads=True`, default timeout. This is the
**only** place that names the browser engine, so a later camoufox swap is one function.

**`selectors.py`** constants:
- `URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"`
- `MATTER_PLACEHOLDER = "M01234"` (the "eg M01234" prompt)
- `DOC_TYPES = ["Exhibits","Key Documents","Other Documents","Transcripts","Recordings"]`
- Timeouts: `DOWNLOAD_TIMEOUT_MS=90_000`, `MODAL_TIMEOUT_MS=12_000`, `CURTAIN_TIMEOUT_MS=8_000`,
  `APP_READY_TIMEOUT_MS=30_000`.

**`scraper.py`** — `UARBScraper(page)` with these methods, each honoring the confirmed mechanics:

- `await goto_and_ready()`: `page.goto(URL, wait_until="domcontentloaded")`, then poll until
  `.fm-textarea`/`.fm-widget` are painted **and** `.v-loading-indicator` is hidden. **Never use
  `networkidle`** (Atmosphere push never idles). No content iframe (target `page` directly; the
  `UIWidgetSet` `about:blank` frame is GWT history, ignore it).
- `await search(matter_number) -> bool`: locate the matter box = the `.fm-textarea` whose
  `.placeholder` contains `MATTER_PLACEHOLDER`; click its inner `.text`, wait ~700ms (server enters
  edit mode), `keyboard.type(matter, delay=60)` — **`fill()` does not work**. Click the Search
  button **chosen by geometry** (nearest by vertical center to the matter field, x to its right;
  ~5 buttons read "Search"). Wait for results signal = body text contains any `"<Type> - <N>"`.
  Return `found`: if no results / a "not found" state appears, return False.
- `await read_metadata_and_counts() -> MatterMetadata`: counts via deterministic regex
  `rf"{type}\s*-\s*(\d+)"` over `document.body.innerText` for all five types (no tab opening
  needed). Metadata via the **LLM** (pass the results-screen label block / `body.innerText` to
  `extract`-style parsing — but use a dedicated `summarize`-independent metadata extraction call, or
  reuse Sonnet structured output) into `MatterMetadata`. Notes the LLM must honor: dates are
  **MM/DD/YYYY**; amount rides **inside** the title line `"{org} - {project} - $amount"`; **Type and
  Category are distinct fields**. Read live — never hardcode literals.
- `await download_type(document_type, limit) -> list[DownloadedDoc]`: open the type's tab button
  (`get_by_role("button", name=document_type)`); **dismiss any modal first** (see guard). If the
  count is 0, return `[]` (caller sends the empty-type reply). Then run the **scroll-collect-dedupe
  download loop**:
  - Maintain `done_docs: set[str]` keyed by doc number (stable identifier; derive from the modal's
    first filename or the row's Doc-No cell — pick ONE and use it for both skip-check and add).
    Loop while `len(done_docs) < limit` and `no_row_progress < 3`.
    - Re-query the visible `Go Get It` buttons (only ~8 render — virtualized Vaadin grid).
    - For each: open its `.v-window` "Download Files" modal (event-wait on modal + a file button
      visible). Read all file-button labels. If this doc is already in `done_docs`, close modal and
      continue (scroll overlap). Otherwise **download every file button in the modal** (each via
      `page.expect_download()`), saving to a temp dir; assemble a `DownloadedDoc`. Add the doc key
      to `done_docs`. Close the modal. Sleep `POLITE_DELAY_S`.
    - **Per-document download is wrapped in the retry taxonomy (§9):** a failed `expect_download`
      retries with backoff (transient), and only after exhausting retries is the document skipped
      and counted as `failed`. A skipped doc does **not** terminate the loop.
    - After exhausting rendered rows, `scroll_doc_list()` (mouse-wheel the `.v-grid`, then wait for
      the Go-Get-It count to stabilize). Termination is keyed on **row progress** (did new,
      non-duplicate filenames appear?), not on download success — so transient download failures
      never end collection early.
  - Apply the **"PDF Only"** filter before the loop (we handle PDFs primarily; non-PDF types exist
    in the corpus and are accepted, classified by magic bytes, but the filter narrows to expected
    files).

**Two mandatory guards (both observed live):**
- **Empty-tab modal:** opening a 0-count tab raises a "No Matching Records" `.v-window` whose
  modality curtain blocks **every** later click until dismissed. A reusable `dismiss_modal()` must
  run before each tab/download action.
- **Virtualized list:** only ~8 rows in the DOM at once — the scroll loop above is mandatory to
  reach up to 10.

**Download facts:** files served from `/fmi/webd/APP/connector/0/<n>/dl/<file>`,
`application/octet-stream`, `content-disposition: attachment`; `<n>` is generated per click (no
pre-fetch / no in-session `request.get()` — plain click-and-catch). Downloads are **sequential
only** (singleton modal + curtain). Files can be **48MB+** → stream to disk, never hold in RAM.

### 7.7 Packager (`app/package/packager.py`)
`async def package(docs, job_id, settings) -> tuple[Literal["attach","link"], str]`
(returns `("attach", zip_path)` or `("link", signed_url)`):
- Build the zip **incrementally on disk** (`zipfile`), adding each downloaded file then deleting
  the source temp file, to cap peak disk/RAM.
- **If `zip_size <= attach_threshold_bytes`** → return `("attach", zip_path)`; the email attaches it.
- **Else** → upload the zip to GCS (`gcs_bucket`, key `jobs/{job_id}.zip`), mint a V4 signed GET URL
  (TTL `signed_url_ttl_hours`), return `("link", signed_url)`. The email links it **and states the
  reason** (too large to attach).
- Filenames in the zip = the documents' real names; de-collide if two share a name.

> **Threshold is on the raw zip, set below 25MB on purpose.** Email size limits (Gmail's 25MB and
> most others) apply to the **base64-encoded** message, and attachments are sent to AgentMail as
> base64 `content` (so the wire payload is ~33% larger than the raw zip). Default
> `attach_threshold_bytes = 18MB` keeps the encoded attachment comfortably under 25MB. AgentMail's
> own attachment ceiling is **not documented** — the threshold is the real gate, not a guess to be
> replaced by a runtime probe.

> **Delivery decision — threshold-gated attach, with a GCS link fallback; threshold is primary.**
> *Why threshold-first (not "just try the attach and fall back"):* AgentMail's docs show the failure
> path for an oversized send is asynchronous — delivery is reported via webhook events
> (`message.sent` → `message.delivered` → `message.bounced`), and a successful `send()` means
> accepted-for-processing, not delivered. So you cannot rely on `send()` throwing for an oversized
> attachment. Worse, AgentMail penalizes bounces hard (permanent block of any bounced recipient +
> an account-level <4% bounce-rate threshold), so letting oversized mail bounce as the "signal"
> would risk blocking the very user being served and damaging sending reputation. The conservative,
> base64-aware threshold is therefore the **primary, deterministic** gate and the thing that keeps
> us from ever triggering a bounce.
> *Secondary net:* wrap `send()` (with the attachment) in a try/except — if AgentMail *does* reject
> oversize synchronously (e.g. a `message.rejected`-style validation error returned on the call),
> catch it and fall back to the GCS link. Cheap insurance; do **not** depend on it firing.
> *Not used:* `message.bounced` as the fallback trigger — too late (async) and reputation-penalized.
> *Future (document, don't build):* subscribe to `message.bounced`/`message.rejected` webhooks for
> observability and a last-resort "send the link as a follow-up" recovery.
> *Requirement note:* attaching by default satisfies challenge req 3a ("the ZIP must be included as
> an attachment"); the link is the fallback for zips that exceed any email limit. *Rejected
> alternatives:* always-link (diverges from 3a for the common small case); multi-zip-split (ugly,
> burns the send-rate cap, some servers strip zips).

### 7.8 — process_job glue: see §4/§7.2.

### 7.9 Store (`app/store/`)
```python
class Store(ABC):
    async def claim_message(self, message_id: str) -> bool:   # True if newly claimed (process it)
    async def save_job(self, job: JobRecord) -> None
    async def load_job(self, message_id: str) -> JobRecord | None
    async def set_status(self, job_id: str, status: str) -> None
    async def get_thread(self, thread_id: str) -> ThreadContext | None
    async def upsert_thread(self, ctx: ThreadContext) -> None
```

Two implementations behind this one interface; **selected by `ENV`** (local → `InMemoryStore`,
prod → `SupabaseStore`). Both back *both* store uses — idempotency and thread context — so the
idempotency logic and the thread-follow-up feature are built and fully tested locally with no
database; provisioning Supabase is a pure adapter swap before deploy (see build order §11).

- **`InMemoryStore` (local default):** plain dicts — one keyed by `message_id` (jobs/idempotency),
  one keyed by `thread_id` (thread context). `claim_message` = check-and-set on the dict (atomic
  enough for single-process local dev). Not durable across restarts — fine for the dev loop, and
  enough to exercise the thread feature's logic (inherit-on-null, explicit-overrides-inherited).
- **`SupabaseStore` (prod):** `claim_message` = `INSERT INTO jobs (message_id, status, inbound)
  VALUES ($1,'processing',$2) ON CONFLICT (message_id) DO NOTHING RETURNING job_id`. A returned row
  = newly claimed (process it); no row = already seen (skip). One atomic statement — the
  `UNIQUE(message_id)` on `jobs` is the idempotency gate (no separate idempotency table needed),
  handling AgentMail + Cloud Tasks retries. Thread context via the `threads` upsert. (Tables in
  `schema.sql`.)
- Thread context keyed by `thread_id`. `get_thread` used during extraction to inherit a null
  `matter_number`/`document_type`; **explicit value in the current email always overrides inherited**
  (set `inherited_*` flags for logging). `upsert_thread` after a successful scrape.

### 7.10 Queue (`app/queue/tasks.py`)
- `inline`: `await process_job(message_id)` directly.
- `tasks`: create a Cloud Task targeting `process_url` (`POST {message_id}`) with an OIDC token
  (`tasks_invoker_sa`). Queue config sets `max_concurrent_dispatches = tasks_max_dispatch` (= Cloud
  Run `max_instances`) and retry config (a few attempts with backoff; rely on `/process` returning
  `5xx` only for retryable failures so user-errors aren't retried).

---

## 8. Observability (`app/observability.py`)
- structlog: JSON renderer in prod, console in local. A `correlation_id` contextvar (= job_id) bound
  for the whole job and emitted on every line. Log the lifecycle: received → classified → extracted
  {matter,type,inherited} → counts → per-doc download results → zip size → delivery → sent/failed.
- On any `scrape` failure: save the Playwright trace + a screenshot + `page.content()` for the
  failing step (prod: on failure only; local: trace always).
- Sentry initialized if `sentry_dsn` set; capture unhandled + `TerminalError`(infra) with the
  correlation id as a tag.

---

## 9. Error taxonomy (`app/errors.py`)
- `RetryableError`: timeouts, 5xx from the site, connection resets, a download that didn't start,
  transient LLM/API errors. → `tenacity` exponential backoff + jitter, ~3 attempts. At the
  `/process` boundary, an unresolved RetryableError returns `5xx` so Cloud Tasks retries the job.
- `TerminalError`: matter-not-found, empty type, invalid/missing request, validation failure. →
  no retry; produce the appropriate user reply; `/process` returns `200`.
- `classify_exception(exc) -> RetryableError | TerminalError` centralizes the mapping.
- **Failure-mode → diagnosis (for the README failure taxonomy):** a scrape timeout that finds **no**
  `"<Type> - <N>"` after search = DOM/selector rot (alert via Sentry); a click that hangs the full
  timeout = an undismissed modal (guard regression); a clean "matter not found" = legitimate user
  error (clarify, no alert).
- Per-document download failures are isolated: retry, then skip-and-report (counted in
  `ScrapeResult.failed`); never fail the whole job for one bad document.

---

## 10. Deployment

- **Dockerfile:** from the Playwright Python base image; `pip install -r requirements.txt`; copy
  `app/`; `CMD uvicorn app.main:app --host 0.0.0.0 --port 8080`.
- **Cloud Run:** memory **2GiB** (bump to 4GiB if large matters OOM — peak = Chromium + current
  download + growing zip, all on tmpfs), CPU 1–2, **concurrency = 1**, **max-instances = 3**,
  request timeout 300s, `--no-cpu-throttling` not required (jobs run inside the request or via
  Tasks). Service account needs: Storage object admin (the bucket), Cloud Tasks enqueuer, and
  **`roles/iam.serviceAccountTokenCreator` on itself** — required to mint V4 signed URLs from Cloud
  Run without a key file (the SA self-signs via the IAM SignBlob API). This is exercised on the
  oversized-zip link path; without it, large-file replies fail at runtime even though small ones
  attach fine — so set it up regardless. Plus a separate invoker SA for the task OIDC with
  `run.invoker` on the service. Supabase is reached via its connection string/keys (Secret Manager →
  env), not GCP IAM.
- **Cloud Tasks:** one queue (`tasks_queue` in `tasks_location`), `max_concurrent_dispatches = 3`.
- **GCS:** one bucket (`gcs_bucket`), uniform access; objects under `jobs/` with a lifecycle rule
  to auto-delete after a few days (align with `signed_url_ttl_hours` so links don't outlive files).
- **Supabase:** one project; apply `schema.sql` (`jobs` + `threads`; `jobs.message_id` UNIQUE is the
  idempotency gate); put the URL + service-role key in env. No emulator — local used `InMemoryStore`.
- **Secrets:** API keys + webhook secret + Supabase key via Secret Manager → env.
- **AgentMail:** point its inbound webhook at `{service}/inbound`; set the signing secret.

---

## 11. Build order (phased; each phase independently testable)

1. **Scaffold** — `config.py`, `models.py`, `observability.py`, FastAPI app with `/health`, the
   `EmailClient`/`LLM`/`Store` interfaces + `FileEmailClient` and a working **`InMemoryStore`** (not
   a stub — real dicts, backs idempotency + thread context). `QUEUE_MODE=inline`. No external
   accounts needed yet.
2. **Scraper** (highest risk — do early) — `browser.py`, `selectors.py`, `scraper.py`. Validate live
   against **M12205** (see §12). This is mostly transcription of the confirmed mechanics + the retry
   wrapper and multi-file/virtualization handling.
3. **LLM** — classify/extract/summarize + prompts; unit-test extraction/clarification on crafted
   emails.
4. **Pipeline inline** — wire `process_job` end to end with `FileEmailClient` (replies land in
   `./outbox/`); exercise the full happy path locally.
5. **Robustness** — backoff/retries, **idempotency wired against the in-memory store**, failure
   email, JSON logging + correlation id, trace-on-failure, Sentry. Plus the **packager** (streaming
   zip + threshold branch + GCS link; unit-test the threshold — the GCS upload can be stubbed
   locally until deploy).
6. **Thread-context follow-up feature** — inherit-on-null + explicit-overrides-inherited, persisted
   via the **in-memory store**. *Built and tested fully here — no database required.* This is the
   differentiator; it lands once the core pipeline is solid, not gated on provisioning.
7. **Supabase adapter + provisioning** — implement `SupabaseStore` against the same `Store`
   interface, create the project, apply `schema.sql`, flip the store via `ENV`/config. A pure swap —
   zero feature-logic change.
8. **AgentMailClient + Cloud Tasks + deploy** — bind AgentMail inbound parse + send + signature
   verification; `queue/tasks.py`, `/process` with OIDC, Dockerfile, Cloud Run + queue + bucket;
   flip `QUEUE_MODE=tasks`; live email test. *If time-constrained, ship `inline` — it already works
   end to end.*
9. **Flex** — conversational acks, in cut order.

> The whole system — thread-reply included — is buildable and demoable locally through phase 6 with
> only Anthropic + AgentMail credentials (and AgentMail only when you wire real inbound in phase 8;
> earlier phases use `FileEmailClient`). Supabase and GCP are deferred to phases 7–8.

---

## 12. Validation / oracle

Smoke-test the scraper against **M12205**. Expect: title "Halifax Regional Water Commission -
Windsor Street Exchange Redevelopment Project", amount in the title line, Type "Capital Expenditure
Approvals", Category "Water", dates 04/07/2025 and 10/23/2025, and live counts (Exhibits 13, Key
Documents 5, Other Documents 42, Transcripts 0, Recordings 0 — **these drift; assert structure and
that values are read live, not the literals**). A 10-document pull from "Other Documents" should
yield 10 distinct documents, 0 duplicates, all valid files. Reference evidence (traces, DOM dumps,
the validated `spike.py`) exists in the spike's `spike-artifacts/`.

End-to-end local test: send an `InboundEmail` fixture through `process_job` with `FileEmailClient`
and assert the reply body and the attachment (or, for an oversized zip, the link + reason) in
`./outbox/`.

---

## 13. Out of scope (document in README as "future improvements")

Heavier durable queue (Celery/SQS, DLQ, load-shedding); warm browser pool / browser-as-a-service for
high volume; anti-detection escalation ladder (stealth → camoufox → residential proxies); scheduled
selector-rot canary; multi-matter / batch requests; per-sender rate limiting; AgentMail
`message.bounced`/`message.rejected`/`message.delivered` webhook handling (delivery observability +
last-resort async "send the link as a follow-up" recovery); the downstream search
index (the actual Regulatory Agent). The README is the final deliverable and must carry: onboarding
(setup, secrets, local-vs-prod run, `QUEUE_MODE`/`ENV`), a repo-structure walkthrough, an ADR-style
**decision log covering every decision** (transport, host, queue, scaling, browser, agentic framing,
error taxonomy, thread follow-up, anti-bot posture, GCS, LLM-metadata, document=row, **state store
(Supabase vs Firestore/SQLite)**, and **delivery = base64-aware size threshold as the primary gate
(attach ≤ threshold, GCS link above), with a synchronous send-failure try/except as a secondary net;
bounce-as-signal rejected on AgentMail-deliverability grounds; always-link and multi-zip-split
rejected**), and this future-improvements list.
```
