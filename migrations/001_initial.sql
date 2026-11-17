CREATE TABLE clients (
    client_id text PRIMARY KEY,
    name text NOT NULL,
    target_cpa_inr numeric(14,2) NOT NULL CHECK (target_cpa_inr > 0),
    target_roas numeric(10,4) NOT NULL CHECK (target_roas > 0),
    daily_budget_cap_inr bigint NOT NULL CHECK (daily_budget_cap_inr >= 0),
    max_shift_pct numeric(6,2) NOT NULL CHECK (max_shift_pct BETWEEN 0 AND 100),
    revision bigint NOT NULL DEFAULT 1
);
CREATE TABLE ad_sets (
    ad_set_id text PRIMARY KEY,
    client_id text NOT NULL REFERENCES clients,
    platform text NOT NULL CHECK (platform IN ('meta', 'google')),
    name text NOT NULL,
    creative_id text NOT NULL,
    UNIQUE (client_id, ad_set_id)
);
CREATE TABLE daily_metrics (
    ad_set_id text NOT NULL REFERENCES ad_sets,
    date date NOT NULL,
    spend_inr numeric(14,2) NOT NULL CHECK (spend_inr >= 0),
    impressions bigint NOT NULL CHECK (impressions >= 0),
    clicks bigint NOT NULL CHECK (clicks >= 0 AND clicks <= impressions),
    conversions bigint NOT NULL CHECK (conversions >= 0),
    revenue_inr numeric(14,2) NOT NULL CHECK (revenue_inr >= 0),
    frequency numeric(10,4) NOT NULL CHECK (frequency >= 0),
    PRIMARY KEY (ad_set_id, date)
);
CREATE TABLE runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id text NOT NULL REFERENCES clients,
    date date NOT NULL,
    phase text NOT NULL DEFAULT 'new'
        CHECK (phase IN ('new','ingested','completed','no_data','failed')),
    imported_rows integer NOT NULL DEFAULT 0,
    summary jsonb NOT NULL DEFAULT '{}',
    error text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (client_id, date)
);
CREATE TABLE llm_calls (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES runs,
    client_id text NOT NULL REFERENCES clients,
    prompt_version text NOT NULL,
    model text NOT NULL,
    input jsonb NOT NULL,
    raw_output text,
    parsed_output jsonb,
    decision jsonb NOT NULL DEFAULT '{"status":"started"}',
    latency_ms integer,
    attempt integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE proposals (
    id uuid PRIMARY KEY,
    client_id text NOT NULL REFERENCES clients,
    ad_set_id text NOT NULL,
    run_id bigint NOT NULL REFERENCES runs,
    llm_call_id bigint NOT NULL REFERENCES llm_calls,
    as_of date NOT NULL,
    payload jsonb NOT NULL,
    snapshot jsonb NOT NULL,
    status text NOT NULL CHECK (status IN (
        'pending_approval','blocked','rejected','approved','executing',
        'retry_pending','executed','stale','failed')),
    reasons jsonb NOT NULL DEFAULT '[]',
    reviewer text,
    rejection_reason text,
    reviewed_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    last_error text,
    receipt jsonb,
    replaces_proposal_id uuid REFERENCES proposals,
    refresh_status text CHECK (refresh_status IN ('pending','completed','no_longer_needed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    executed_at timestamptz,
    FOREIGN KEY (client_id, ad_set_id) REFERENCES ad_sets(client_id, ad_set_id),
    CHECK (status <> 'rejected' OR length(trim(rejection_reason)) > 0)
);
-- Review can change status, never the action that was approved.
CREATE FUNCTION protect_proposal_payload() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.payload, NEW.snapshot, NEW.client_id, NEW.ad_set_id, NEW.as_of,
        NEW.run_id, NEW.llm_call_id, NEW.replaces_proposal_id)
        IS DISTINCT FROM
       (OLD.payload, OLD.snapshot, OLD.client_id, OLD.ad_set_id, OLD.as_of,
        OLD.run_id, OLD.llm_call_id, OLD.replaces_proposal_id) THEN
        RAISE EXCEPTION 'Proposal content is immutable; create a replacement';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER immutable_proposal BEFORE UPDATE ON proposals
    FOR EACH ROW EXECUTE FUNCTION protect_proposal_payload();

CREATE TABLE audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    proposal_id uuid REFERENCES proposals,
    client_id text REFERENCES clients,
    kind text NOT NULL,
    actor text NOT NULL,
    detail jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE mock_ad_sets (
    ad_set_id text PRIMARY KEY,
    client_id text NOT NULL,
    daily_budget_inr bigint NOT NULL CHECK (daily_budget_inr >= 0),
    status text NOT NULL CHECK (status IN ('active','paused')),
    creative_id text NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (client_id, ad_set_id) REFERENCES ad_sets(client_id, ad_set_id)
);
CREATE TABLE mock_operations (
    operation_key text PRIMARY KEY,
    client_id text NOT NULL REFERENCES clients,
    request_hash text NOT NULL,
    actor text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE mock_controls (
    id integer PRIMARY KEY CHECK (id = 1),
    calls bigint NOT NULL DEFAULT 0,
    rate_limit_after bigint CHECK (rate_limit_after >= 0),
    remaining_failures integer CHECK (remaining_failures >= 0),
    retry_after integer NOT NULL DEFAULT 1 CHECK (retry_after >= 0)
);
INSERT INTO mock_controls(id) VALUES (1);

