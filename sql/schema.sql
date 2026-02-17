CREATE TABLE IF NOT EXISTS repositories (
    node_id     TEXT        PRIMARY KEY,
    full_name   TEXT        NOT NULL UNIQUE,
    name        TEXT        NOT NULL,
    owner_login TEXT        NOT NULL,
    stars       INTEGER     NOT NULL DEFAULT 0,
    scraped_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    extra       JSONB       NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_repos_stars   ON repositories(stars DESC);
CREATE INDEX IF NOT EXISTS idx_repos_owner   ON repositories(owner_login);
CREATE INDEX IF NOT EXISTS idx_repos_scraped ON repositories(scraped_at);
CREATE INDEX IF NOT EXISTS idx_repos_extra   ON repositories USING GIN(extra);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id          SERIAL      PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    total_repos INTEGER     NOT NULL DEFAULT 0,
    status      TEXT        NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'success', 'failed')),
    error_msg   TEXT
);

CREATE OR REPLACE VIEW repos_view AS
SELECT
    node_id,
    full_name,
    name,
    owner_login,
    stars,
    scraped_at,
    extra->>'description'           AS description,
    extra->>'primary_language'      AS primary_language,
    (extra->>'is_private')::boolean AS is_private,
    (extra->>'created_at')::timestamptz AS created_at,
    (extra->>'updated_at')::timestamptz AS updated_at
FROM repositories;