CREATE TABLE IF NOT EXISTS accounts (
    id             TEXT PRIMARY KEY,
    currency       CHAR(3) NOT NULL,
    balance        BIGINT NOT NULL DEFAULT 0,
    allow_negative BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT balance_non_negative CHECK (allow_negative OR balance >= 0)
);

CREATE TABLE IF NOT EXISTS transactions (
    id               UUID PRIMARY KEY,
    idempotency_key  TEXT NOT NULL,
    client_id        TEXT NOT NULL,
    from_account     TEXT NOT NULL,
    to_account       TEXT NOT NULL,
    amount           BIGINT NOT NULL CHECK (amount > 0),
    currency         CHAR(3) NOT NULL,
    reference        TEXT,
    status           TEXT NOT NULL CHECK (status IN ('pending', 'posted', 'rejected')),
    reason           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_at        TIMESTAMPTZ,
    UNIQUE (client_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id             BIGSERIAL PRIMARY KEY,
    transaction_id UUID NOT NULL REFERENCES transactions(id),
    account_id     TEXT NOT NULL REFERENCES accounts(id),
    direction      TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount         BIGINT NOT NULL CHECK (amount > 0),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ledger_entries_account_idx ON ledger_entries (account_id);
CREATE INDEX IF NOT EXISTS ledger_entries_txn_idx ON ledger_entries (transaction_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id        BIGSERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details   JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Ledger entries and the audit log are append-only.
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

DROP TRIGGER IF EXISTS ledger_entries_append_only ON ledger_entries;
CREATE TRIGGER ledger_entries_append_only BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
