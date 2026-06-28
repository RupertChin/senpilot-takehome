-- schema.sql — Supabase (Postgres) tables for the agent's state store.
-- Apply once to your Supabase project (SQL editor, or `psql "$DATABASE_URL" -f schema.sql`).
-- gen_random_uuid() is built in on Supabase/PG13+. If missing: create extension if not exists pgcrypto;

-- ── jobs ─────────────────────────────────────────────────────────────────────
-- One row per inbound email. The UNIQUE(message_id) constraint IS the idempotency gate,
-- so a separate idempotency table is unnecessary — claim and create the job in one statement:
--
--   INSERT INTO jobs (message_id, status, inbound)
--   VALUES ($1, 'processing', $2::jsonb)
--   ON CONFLICT (message_id) DO NOTHING
--   RETURNING job_id;
--
-- A returned row  -> newly claimed; process it (job_id is also the user-facing reference id).
-- No row returned -> already seen (AgentMail/Cloud Tasks retry); return 200 and skip.
create table if not exists jobs (
    job_id      uuid        primary key default gen_random_uuid(),
    message_id  text        not null unique,
    status      text        not null default 'processing'
                              check (status in ('processing', 'done', 'failed')),
    inbound     jsonb       not null,            -- the serialized InboundEmail
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- ── threads ──────────────────────────────────────────────────────────────────
-- Per-conversation context for the follow-up feature, keyed by AgentMail thread id.
-- On a request whose matter_number / document_type is null, inherit from here
-- (explicit value in the current email always overrides inherited). Upsert after a successful scrape.
create table if not exists threads (
    thread_id           text        primary key,
    last_matter_number  text,
    last_document_type  text,
    updated_at          timestamptz not null default now()
);

-- updated_at is maintained by the application on each write (kept out of triggers for simplicity).
-- Upsert pattern for threads:
--   INSERT INTO threads (thread_id, last_matter_number, last_document_type, updated_at)
--   VALUES ($1, $2, $3, now())
--   ON CONFLICT (thread_id) DO UPDATE
--     SET last_matter_number = excluded.last_matter_number,
--         last_document_type = excluded.last_document_type,
--         updated_at         = now();
