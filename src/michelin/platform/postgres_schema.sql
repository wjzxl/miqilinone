-- P1 持久化脊：PostgreSQL 16 + pgvector
-- 与 SQLite 版保持同一套表/列契约，上层 ChiefOfStaff 一行不改即可切换。
-- append-only 由 trigger 强制：event / source_document / artifact / verdict 禁 UPDATE/DELETE。

CREATE TABLE IF NOT EXISTS inbox_event (
    event_id       TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    signature_ok   BOOLEAN NOT NULL,
    payload_hash   TEXT NOT NULL,
    received_at    TEXT NOT NULL,
    status         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_document (
    source_hash    TEXT PRIMARY KEY,
    source_system  TEXT NOT NULL,
    source_uri     TEXT NOT NULL,
    raw_bytes      BYTEA NOT NULL,
    occurred_at    TEXT NOT NULL,
    ingested_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task (
    task_id        TEXT PRIMARY KEY,
    parent_id      TEXT,
    intent         TEXT NOT NULL,
    source_refs    TEXT NOT NULL,
    evidence_spec  TEXT NOT NULL,
    risk_level     TEXT NOT NULL,
    budget         DOUBLE PRECISION NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_run (
    run_id          TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    actor           TEXT NOT NULL,
    role            TEXT NOT NULL,
    model_family    TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    prompt_hash     TEXT,
    semconv_version TEXT,
    tool_policy     TEXT,
    started_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    seq                  BIGSERIAL PRIMARY KEY,
    run_id               TEXT NOT NULL,
    task_id              TEXT NOT NULL,
    type                 TEXT NOT NULL,
    actor                TEXT NOT NULL,
    envelope_json        TEXT NOT NULL,
    payload_json         TEXT NOT NULL,
    idempotency_key      TEXT NOT NULL UNIQUE,
    side_effect_receipt  TEXT,
    prev_hash            TEXT NOT NULL,
    hash_chain           TEXT NOT NULL,
    occurred_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_run ON event(run_id, seq);

CREATE TABLE IF NOT EXISTS artifact (
    artifact_hash  TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    path           TEXT NOT NULL,
    content_ref    TEXT NOT NULL,
    produced_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verdict (
    verdict_id       TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    verifier_run_id  TEXT NOT NULL,
    head_sha         TEXT NOT NULL,
    checks_json      TEXT NOT NULL,
    evidence_refs    TEXT NOT NULL,
    result           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    seq              BIGSERIAL NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_verdict_head ON verdict(head_sha, seq);

CREATE TABLE IF NOT EXISTS sample_review (
    review_id   TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    reviewer    TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    root_cause  TEXT NOT NULL,
    rule_ref    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guardrail_rule (
    rule_id            TEXT PRIMARY KEY,
    kind               TEXT NOT NULL,
    target             TEXT NOT NULL,
    body               TEXT NOT NULL,
    mutation_test_ref  TEXT NOT NULL,
    version            INTEGER NOT NULL,
    enabled            BOOLEAN NOT NULL,
    created_at         TEXT NOT NULL,
    source_review_id   TEXT
);

-- P1 新增：webhook 投递台账（DLQ / 对账）与人工审批门
CREATE TABLE IF NOT EXISTS inbox_delivery (
    delivery_id     TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    signature_ok    BOOLEAN NOT NULL,
    timestamp_ok    BOOLEAN NOT NULL,
    payload_hash    TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    status          TEXT NOT NULL,
    failure_reason  TEXT,
    workflow_id     TEXT,
    replay_count    INTEGER NOT NULL DEFAULT 0,
    raw_ref         TEXT
);

ALTER TABLE inbox_delivery ADD COLUMN IF NOT EXISTS raw_ref TEXT;

CREATE TABLE IF NOT EXISTS approval_gate (
    gate_id     TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    topic       TEXT NOT NULL,
    status      TEXT NOT NULL,
    decision    TEXT,
    decided_by  TEXT,
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);

CREATE OR REPLACE FUNCTION michelin_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only: % forbids %', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

DROP TRIGGER IF EXISTS trg_event_append_only ON event;
CREATE TRIGGER trg_event_append_only BEFORE UPDATE OR DELETE ON event
    FOR EACH ROW EXECUTE FUNCTION michelin_append_only();

DROP TRIGGER IF EXISTS trg_source_document_append_only ON source_document;
CREATE TRIGGER trg_source_document_append_only BEFORE UPDATE OR DELETE ON source_document
    FOR EACH ROW EXECUTE FUNCTION michelin_append_only();

DROP TRIGGER IF EXISTS trg_artifact_append_only ON artifact;
CREATE TRIGGER trg_artifact_append_only BEFORE UPDATE OR DELETE ON artifact
    FOR EACH ROW EXECUTE FUNCTION michelin_append_only();

DROP TRIGGER IF EXISTS trg_verdict_append_only ON verdict;
CREATE TRIGGER trg_verdict_append_only BEFORE UPDATE OR DELETE ON verdict
    FOR EACH ROW EXECUTE FUNCTION michelin_append_only();
