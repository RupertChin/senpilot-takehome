# CLAUDE.md — build orientation

You are building the **Senpilot Regulatory Email Agent**. Read this first, then work from the spec.

## Source of truth
- **`IMPLEMENTATION_SPEC.md`** — the authoritative spec. Build to it. Schemas, interfaces, config,
  flow, error taxonomy, email behaviors, deployment, and the decision log all live there.
- **`reference/spike.py`** — the **validated** scraper discovery script. Its selectors and the
  two-step download + scroll loop are confirmed working against the live site. **Lift its selectors
  and timing verbatim** when writing `app/scrape/` — do not re-derive the scraping approach.
- **`reference/FINDINGS.md`** — the spike report (download mechanism, gotchas, the M12205 oracle).

## Golden rules
1. **Follow the build order in spec §11.** Scaffold → **scraper first** (highest risk) → LLM →
   pipeline (inline) → packager → store → AgentMail → (deploy later).
2. **Validate the scraper against matter `M12205` before building around it** (spec §12 oracle).
   Counts drift — assert structure and that values are read live, not the literal numbers.
3. **Reuse the spike's scraping mechanics, don't reinvent them:** content-anchored locators (never
   generated ids); poll for painted FileMaker widgets for readiness (**never `networkidle`**); the
   two-step "Go Get It" → modal → file-button download; the **empty-tab "No Matching Records" modal
   guard** (dismiss before every tab/download action); virtualized-list scroll-collect-dedupe to
   reach up to 10 documents.
4. **Local dev needs no GCP.** Run with `ENV=local`, `QUEUE_MODE=inline`, and `FileEmailClient`
   (replies are written to `./outbox/`). Cloud Tasks is skipped in inline mode; the GCS large-file
   path may be stubbed until deploy. The whole flow is testable locally end to end.
5. **Bind AgentMail against its live docs — do not guess its API.** It is niche; your training data
   will be wrong. Read `https://docs.agentmail.to/llms-full.txt` (or the SDK) for `messages.send`,
   attachments, webhook payloads, and signature verification. Map them onto the `EmailClient`
   interface (spec §7.3); keep all AgentMail-specific code in `app/email/agentmail.py`.
6. **Be polite to the live government site while iterating:** reuse the spike's pacing
   (`POLITE_DELAY_S`, sequential downloads, single matter). Do not hammer it during dev.
7. **Decisions are settled** (spec §0 and §6, each with rationale + rejected alternatives). Don't
   relitigate them. If you believe one is wrong, **stop and flag it** — don't silently diverge.

## Models
Classify → `claude-haiku-4-5`. Extract + summarize → `claude-sonnet-4-6`. Behind the `LLM`
interface so swappable. Summary numbers are authoritative facts, never model-invented (spec §7.4).

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env        # fill ANTHROPIC, AGENTMAIL, SUPABASE values
uvicorn app.main:app --reload
```
Secrets live in `.env` (never commit it). `.env.example` lists every variable. Apply `schema.sql`
to your Supabase project for the store tables.

## Definition of done (local)
Scraper passes the M12205 oracle; an `InboundEmail` fixture run through `process_job` with
`FileEmailClient` produces the correct reply (and attachment, or link+reason for an oversized zip)
in `./outbox/`. Deployment (Cloud Run + Cloud Tasks + GCS) is a later step.
