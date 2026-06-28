# Deploy guide — Cloud Run + Cloud Tasks + GCS (Stage 12)

A step-by-step walkthrough to stand up the GCP infrastructure and deploy the agent. The app code
for prod (Cloud Tasks dispatch, `/process` OIDC verification, the GCS signed-URL uploader, the
Dockerfile) is already implemented — this guide is the infra you provision around it.

You'll do everything in **§1–§7**; once it's up I (or you) run the **validation** in §8. The only
thing I need from you to deploy on your behalf is an authenticated `gcloud` in this environment
(`gcloud auth login`) **or** a service-account key with deploy rights — see §0.

---

## 0. What I need from you

- A **GCP project** with **billing enabled** (Cloud Run + Tasks + GCS are pay-as-you-go; this
  workload is tiny — pennies).
- `gcloud` authenticated as a principal that can create SAs, buckets, queues, and deploy to Cloud
  Run (Owner or Editor is simplest for setup). Either:
  - run `gcloud auth login` (and `gcloud config set project <PROJECT_ID>`) in this session, **or**
  - hand me a deploy service-account key JSON and I'll `gcloud auth activate-service-account`.

You already have the application secrets (in your local `.env`): `ANTHROPIC_API_KEY`,
`AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX`, `AGENTMAIL_WEBHOOK_SECRET`, `SUPABASE_URL`, `SUPABASE_KEY`.
We'll load these into Secret Manager.

Set these shell variables once (used throughout):

```bash
export PROJECT_ID="your-project-id"
export REGION="us-central1"          # Cloud Run + Tasks region
export SERVICE="senpilot-agent"
export BUCKET="${PROJECT_ID}-senpilot-zips"
export QUEUE="senpilot-jobs"
gcloud config set project "$PROJECT_ID"
```

---

## 1. Enable APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudtasks.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com
```

## 2. GCS bucket (oversized-zip delivery)

Uniform bucket-level access; a lifecycle rule auto-deletes objects under `jobs/` after a few days
so links don't outlive their files (align with `SIGNED_URL_TTL_HOURS=72` → 4 days is safe).

```bash
gcloud storage buckets create "gs://${BUCKET}" \
  --location="$REGION" --uniform-bucket-level-access

cat > /tmp/lifecycle.json <<'JSON'
{"rule":[{"action":{"type":"Delete"},"condition":{"age":4,"matchesPrefix":["jobs/"]}}]}
JSON
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file=/tmp/lifecycle.json
```

## 3. Cloud Tasks queue

`max-concurrent-dispatches = 3` must equal Cloud Run `max-instances` (concurrency=1, so one heavy
job per instance). A few retry attempts with backoff; `/process` returns 5xx only for retryable
failures.

```bash
gcloud tasks queues create "$QUEUE" --location="$REGION" \
  --max-concurrent-dispatches=3 \
  --max-attempts=5 --min-backoff=10s --max-backoff=300s
```

## 4. Service accounts

Two SAs (spec §10): the **runtime** SA the service runs as, and a separate **invoker** SA that
Cloud Tasks uses to mint the OIDC token on each task.

```bash
# Runtime SA — what the Cloud Run service runs as.
gcloud iam service-accounts create senpilot-run --display-name="Senpilot runtime"
export RUN_SA="senpilot-run@${PROJECT_ID}.iam.gserviceaccount.com"

# Invoker SA — Cloud Tasks signs each task's OIDC token as this principal.
gcloud iam service-accounts create senpilot-invoker --display-name="Senpilot task invoker"
export INVOKER_SA="senpilot-invoker@${PROJECT_ID}.iam.gserviceaccount.com"
```

Grant the runtime SA exactly what it needs:

```bash
# Read/write the bucket (upload the zip, generate signed URLs).
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${RUN_SA}" --role="roles/storage.objectAdmin"

# Enqueue Cloud Tasks.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUN_SA}" --role="roles/cloudtasks.enqueuer"

# Sign V4 URLs WITHOUT a key file: the SA must be able to self-sign via IAM SignBlob.
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" \
  --member="serviceAccount:${RUN_SA}" --role="roles/iam.serviceAccountTokenCreator"
```

The runtime SA enqueues tasks that carry an OIDC token impersonating the **invoker** SA — so it
must be allowed to *act as* the invoker SA (`iam.serviceAccounts.actAs`, in `serviceAccountUser`),
and the Cloud Tasks service agent must be able to mint that token at dispatch time:

```bash
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# The runtime SA may set the invoker SA as a task's OIDC identity.
gcloud iam service-accounts add-iam-policy-binding "$INVOKER_SA" \
  --member="serviceAccount:${RUN_SA}" --role="roles/iam.serviceAccountUser"

# The Cloud Tasks service agent mints the OIDC token as the invoker SA when dispatching.
gcloud iam service-accounts add-iam-policy-binding "$INVOKER_SA" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-cloudtasks.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"
```

## 5. Secrets → Secret Manager

Load the values from your `.env` (run from the repo root). This reads them without echoing:

```bash
set -a; source .env; set +a
for s in ANTHROPIC_API_KEY AGENTMAIL_API_KEY AGENTMAIL_INBOX AGENTMAIL_WEBHOOK_SECRET SUPABASE_URL SUPABASE_KEY; do
  printf '%s' "${!s}" | gcloud secrets create "$s" --data-file=- 2>/dev/null \
    || printf '%s' "${!s}" | gcloud secrets versions add "$s" --data-file=-
done

# Let the runtime SA read them.
for s in ANTHROPIC_API_KEY AGENTMAIL_API_KEY AGENTMAIL_INBOX AGENTMAIL_WEBHOOK_SECRET SUPABASE_URL SUPABASE_KEY; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${RUN_SA}" --role="roles/secretmanager.secretAccessor"
done
```

## 6. First deploy (to get the service URL)

Deploy once in **inline** mode to obtain the URL, then we flip to tasks mode in §7 (the Cloud Task
target `PROCESS_URL` is only knowable after the first deploy). Builds from source via Cloud Build.

```bash
gcloud run deploy "$SERVICE" \
  --source . --region "$REGION" \
  --service-account "$RUN_SA" \
  --memory 2Gi --cpu 2 --concurrency 1 --max-instances 3 --timeout 300 \
  --allow-unauthenticated \
  --update-secrets=ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,AGENTMAIL_API_KEY=AGENTMAIL_API_KEY:latest,AGENTMAIL_INBOX=AGENTMAIL_INBOX:latest,AGENTMAIL_WEBHOOK_SECRET=AGENTMAIL_WEBHOOK_SECRET:latest,SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_KEY=SUPABASE_KEY:latest \
  --set-env-vars=QUEUE_MODE=inline,GCP_PROJECT=${PROJECT_ID},GCS_BUCKET=${BUCKET},TASKS_QUEUE=${QUEUE},TASKS_LOCATION=${REGION},TASKS_INVOKER_SA=${INVOKER_SA}

export SERVICE_URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "Service URL: $SERVICE_URL"
```

> `ENV` is auto-detected as `prod` on Cloud Run (the platform injects `K_SERVICE`) → `SupabaseStore`
> + `AgentMailClient` + headless Chromium.

**Access model — a deliberate divergence from spec §10.** Spec §10 envisions a *private* service
where only the invoker SA has `run.invoker`. But `/inbound` must be reachable by **AgentMail**,
which posts a plain HTTPS webhook with **no Google OIDC token** — and Cloud Run authentication is
per-*service*, not per-*path*, so we can't require platform auth for `/process` while leaving
`/inbound` open. So the service is made platform-public and security is enforced **in-app**:
`/inbound` by the **HMAC webhook signature** (401 on a bad signature) and `/process` by the
**in-app OIDC check** (audience + invoker-SA principal). Net exposure: an unauthenticated caller can
reach the handlers but cannot get either to act without a valid signature / OIDC token.
(`--allow-unauthenticated` above is what makes the service reachable; it grants `allUsers` →
`run.invoker`.)

```bash
gcloud run services add-iam-policy-binding "$SERVICE" --region "$REGION" \
  --member="allUsers" --role="roles/run.invoker"
```

## 7. Flip to tasks mode + wire OIDC

Now that the URL exists, set `PROCESS_URL` and switch `QUEUE_MODE=tasks`, and let the invoker SA be
used for the task OIDC token.

```bash
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars=QUEUE_MODE=tasks,PROCESS_URL=${SERVICE_URL}/process
```

(The actAs bindings for the task OIDC were granted in §4.) The app verifies, in `/process`, that
the token's audience is `PROCESS_URL` and its principal is `TASKS_INVOKER_SA` — so only Cloud Tasks
(acting as the invoker SA) can reach `/process`.

## 8. Point AgentMail at the service

In the AgentMail portal, set the webhook endpoint to:

```
${SERVICE_URL}/inbound      (event: message.received)
```

The signing secret you already configured is the `AGENTMAIL_WEBHOOK_SECRET` we loaded — the app
HMAC-verifies every inbound POST and `401`s on a bad signature.

---

## Validation

```bash
# health
curl -s "${SERVICE_URL}/health"          # {"status":"ok"}

# /process rejects an unsigned/unauthenticated call (no OIDC token) -> 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST "${SERVICE_URL}/process" \
  -H 'content-type: application/json' -d '{"message_id":"x"}'   # 401

# real end-to-end: email your matter request to your AgentMail inbox address, e.g.
#   "Please send the Other Documents for M12205"
# then watch the logs:
gcloud run services logs read "$SERVICE" --region "$REGION" --limit=100
```

Expected log lifecycle: `job_received → classified → extracted → counts_read → metadata_extracted
→ doc_downloaded… → packaged → agentmail_reply_sent → job_done`, and a reply (with the ZIP, or a
GCS link for an oversized matter) lands in your mailbox. Confirm an oversized matter produces a
working signed link (`storage.googleapis.com/...`), exercising the SignBlob path.

---

## Notes / gotchas

- **Two-phase deploy is intentional:** `PROCESS_URL` (the Cloud Task target) only exists after the
  first deploy, so we deploy inline → capture URL → flip to tasks.
- **Tunables default unless set.** `MAX_DOCUMENTS`, `DOWNLOAD_TIMEOUT_S`, `ATTACH_THRESHOLD_BYTES`,
  `SIGNED_URL_TTL_HOURS`, `POLITE_DELAY_S`, and the model ids are **not** in the deploy command
  above, so the service uses their `config.py` defaults (the container never reads `.env`). To set
  or change one in prod, add it to `--set-env-vars` or run a live
  `gcloud run services update $SERVICE --region $REGION --update-env-vars=MAX_DOCUMENTS=5` — no
  redeploy of code needed. See the README "Tunables" table.
- **Memory:** 2 GiB handles Chromium + a download + the growing zip on tmpfs. Bump to 4 GiB if a
  very large matter OOMs.
- **Signed URLs need no key file:** the runtime SA self-signs via IAM SignBlob — that's the
  `serviceAccountTokenCreator on itself` binding in §4. Without it, small attachments work but
  oversized-zip links fail at runtime, so it's set up regardless.
- **Durability:** in tasks mode an unresolved `RetryableError` returns 5xx so Cloud Tasks retries;
  `TerminalError`/user-errors return 200 (no retry). One open item (tracked): if `save_job` ever
  fails after the idempotency claim, an AgentMail re-delivery would skip the now-claimed message —
  acceptable for this scale, hardened later by making claim+save atomic.
- **Teardown:** `gcloud run services delete $SERVICE`, `gcloud tasks queues delete $QUEUE`,
  `gcloud storage rm -r gs://$BUCKET`, delete the two SAs and the secrets.
