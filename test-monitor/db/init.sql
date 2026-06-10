-- Schema (matches production gateway/models.py exactly)
CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    username    VARCHAR(64)  UNIQUE NOT NULL,
    email       VARCHAR(128) UNIQUE NOT NULL,
    department  VARCHAR(64)  NOT NULL,
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    is_admin    BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS api_keys (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER      NOT NULL REFERENCES users(id),
    key_hash     VARCHAR(128) UNIQUE NOT NULL,
    key_prefix   VARCHAR(16)  NOT NULL,
    label        VARCHAR(128) NOT NULL DEFAULT 'default',
    metadata     TEXT,
    is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS request_logs (
    id                SERIAL PRIMARY KEY,
    user_id           INTEGER      REFERENCES users(id),
    api_key_prefix    VARCHAR(16),
    model             VARCHAR(128) NOT NULL,
    node_ip           VARCHAR(64),
    prompt_tokens     INTEGER      NOT NULL DEFAULT 0,
    completion_tokens INTEGER      NOT NULL DEFAULT 0,
    latency_ms        INTEGER      NOT NULL DEFAULT 0,
    queue_ms          INTEGER      NOT NULL DEFAULT 0,
    status_code       INTEGER      NOT NULL,
    error_message     TEXT,
    streaming         BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── Users ────────────────────────────────────────────────────────────────────
INSERT INTO users (username, email, department, is_active, is_admin) VALUES
    ('admin',         'admin@ongc.co.in',          'IT',          true, true),
    ('rajan.kumar',   'rajan.kumar@ongc.co.in',    'Exploration', true, false),
    ('priya.sharma',  'priya.sharma@ongc.co.in',   'Drilling',    true, false),
    ('amit.singh',    'amit.singh@ongc.co.in',     'Production',  true, false),
    ('kavita.nair',   'kavita.nair@ongc.co.in',    'IT',          true, false),
    ('suresh.rao',    'suresh.rao@ongc.co.in',     'Geoscience',  true, false),
    ('deepa.iyer',    'deepa.iyer@ongc.co.in',     'Reservoir',   true, false),
    ('vikram.joshi',  'vikram.joshi@ongc.co.in',   'Drilling',    true, false),
    ('anita.patel',   'anita.patel@ongc.co.in',    'HSE',         true, false),
    ('rohit.gupta',   'rohit.gupta@ongc.co.in',    'Finance',     true, false)
ON CONFLICT DO NOTHING;

-- ── API keys (hashes are dummy — this is test data only) ────────────────────
INSERT INTO api_keys (user_id, key_hash, key_prefix, label, is_active) VALUES
    (1,  'hash_admin',  'sk-ongc-adm0', 'admin-bootstrap',  true),
    (2,  'hash_raj',    'sk-ongc-raj1', 'exploration-app',  true),
    (3,  'hash_pri',    'sk-ongc-pri2', 'drilling-reports', true),
    (4,  'hash_ami',    'sk-ongc-ami3', 'prod-analysis',    true),
    (5,  'hash_kav',    'sk-ongc-kav4', 'it-automation',    true),
    (6,  'hash_sur',    'sk-ongc-sur5', 'geo-analysis',     true),
    (7,  'hash_dee',    'sk-ongc-dee6', 'reservoir-sim',    true),
    (8,  'hash_vik',    'sk-ongc-vik7', 'drilling-ops',     true),
    (9,  'hash_ani',    'sk-ongc-ani8', 'hse-reports',      true),
    (10, 'hash_roh',    'sk-ongc-roh9', 'finance-ai',       true)
ON CONFLICT DO NOTHING;

-- ── Request logs — 600 rows spread across last 7 days ────────────────────────
INSERT INTO request_logs
    (user_id, api_key_prefix, model, node_ip,
     prompt_tokens, completion_tokens, latency_ms, queue_ms,
     status_code, streaming, created_at)
SELECT
    uid,
    CASE uid
        WHEN 1  THEN 'sk-ongc-adm0'
        WHEN 2  THEN 'sk-ongc-raj1'
        WHEN 3  THEN 'sk-ongc-pri2'
        WHEN 4  THEN 'sk-ongc-ami3'
        WHEN 5  THEN 'sk-ongc-kav4'
        WHEN 6  THEN 'sk-ongc-sur5'
        WHEN 7  THEN 'sk-ongc-dee6'
        WHEN 8  THEN 'sk-ongc-vik7'
        WHEN 9  THEN 'sk-ongc-ani8'
        ELSE         'sk-ongc-roh9'
    END,
    'ongc-llm',
    node_ip,
    (random() * 430 + 20)::int,
    (random() * 560 + 40)::int,
    -- log-normal latency: mostly 2-15 s, occasional slow ones
    least(60000, greatest(500, (exp(random() * 1.8 + 7.5))::int)),
    (random() * 300)::int,
    CASE WHEN random() < 0.96 THEN 200 ELSE 500 END,
    random() < 0.4,
    NOW() - (random() * INTERVAL '7 days')
FROM (
    SELECT
        (random() * 9 + 1)::int                      AS uid,
        (ARRAY[
            '10.208.211.62','10.208.211.54',
            '10.208.211.59','10.208.211.64'
        ])[floor(random() * 4 + 1)::int]             AS node_ip
    FROM generate_series(1, 600)
) sub;
