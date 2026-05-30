CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY NOT NULL,
    data TEXT NOT NULL,
    state TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    vehicle_vin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_terminal INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cases_state ON cases(state);
CREATE INDEX IF NOT EXISTS idx_cases_customer_phone ON cases(customer_phone);
CREATE INDEX IF NOT EXISTS idx_cases_vehicle_vin ON cases(vehicle_vin);
CREATE INDEX IF NOT EXISTS idx_cases_created_at ON cases(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cases_is_terminal ON cases(is_terminal);

-- ---------------------------------------------------------------------------
-- Outbound message queue.
--
-- Single point of egress for every customer-facing send. The reducer/call
-- session enqueues here; an async worker drains it FIFO, honouring two
-- gates (SMS consent, business hours). On a clean send the row moves to
-- 'sent' with the Twilio MessageSid recorded for audit; on a permanent
-- block (consent revoked) it goes 'blocked'; on max-attempts of transient
-- failures it goes 'failed'.
--
-- States:
--   pending   — waiting in the queue (the drain set)
--   in_flight — worker has claimed it, send in progress
--   sent      — Twilio accepted the send
--   blocked   — consent / policy refused; terminal
--   failed    — max_attempts exhausted; terminal
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbound_queue (
    item_id      TEXT PRIMARY KEY NOT NULL,
    case_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,        -- 'sms_text' (only value in v1)
    to_phone     TEXT NOT NULL,
    body         TEXT NOT NULL,
    state        TEXT NOT NULL,        -- pending|in_flight|sent|blocked|failed
    enqueued_at  TEXT NOT NULL,        -- ISO 8601 UTC
    hold_until   TEXT NOT NULL,        -- ISO 8601 UTC; worker won't claim before this
    claimed_at   TEXT,                 -- ISO 8601 UTC when worker took it
    sent_at      TEXT,                 -- ISO 8601 UTC when Twilio accepted
    twilio_sid   TEXT NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error   TEXT NOT NULL DEFAULT ''
);

-- Hot path for the worker: "pending items whose hold has elapsed, oldest first."
CREATE INDEX IF NOT EXISTS idx_outbound_queue_drain
    ON outbound_queue(state, hold_until, enqueued_at);
CREATE INDEX IF NOT EXISTS idx_outbound_queue_case_id
    ON outbound_queue(case_id, enqueued_at);
CREATE INDEX IF NOT EXISTS idx_outbound_queue_state
    ON outbound_queue(state);
