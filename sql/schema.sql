
CREATE TABLE IF NOT EXISTS repositories (
    node_id         TEXT        PRIMARY KEY,          
    name_with_owner TEXT        NOT NULL UNIQUE,      
    name            TEXT        NOT NULL,
    owner_login     TEXT        NOT NULL,
    description     TEXT,
    primary_language TEXT,
    is_private      BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ,                      -- GitHub's last updated timestamp
    crawled_at      TIMESTAMPTZ NOT NULL DEFAULT NOW() -- when WE last crawled it
);

CREATE INDEX IF NOT EXISTS idx_repos_owner ON repositories(owner_login);
CREATE INDEX IF NOT EXISTS idx_repos_crawled_at ON repositories(crawled_at);


CREATE TABLE IF NOT EXISTS repository_stars (
    node_id         TEXT        NOT NULL REFERENCES repositories(node_id) ON DELETE CASCADE,
    star_count      INTEGER     NOT NULL CHECK (star_count >= 0),
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (node_id, recorded_at)
);


CREATE INDEX IF NOT EXISTS idx_stars_node_recorded ON repository_stars(node_id, recorded_at DESC);

-- Convenience view: latest star count per repo (used by downstream consumers)
CREATE OR REPLACE VIEW latest_star_counts AS
SELECT DISTINCT ON (rs.node_id)
    r.node_id,
    r.name_with_owner,
    r.owner_login,
    r.name,
    rs.star_count,
    rs.recorded_at
FROM repository_stars rs
JOIN repositories r USING (node_id)
ORDER BY rs.node_id, rs.recorded_at DESC;


CREATE TABLE IF NOT EXISTS crawl_runs (
    id              SERIAL      PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    repos_fetched   INTEGER     NOT NULL DEFAULT 0,
    status          TEXT        NOT NULL DEFAULT 'running' 
        CHECK (status IN ('running', 'success', 'failed')),
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    node_id         TEXT        PRIMARY KEY,
    repo_node_id    TEXT        NOT NULL REFERENCES repositories(node_id) ON DELETE CASCADE,
    number          INTEGER     NOT NULL,
    title           TEXT,
    state           TEXT,                            
    author_login    TEXT,
    created_at      TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    crawled_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (repo_node_id, number)
);

CREATE TABLE IF NOT EXISTS pull_requests (
    node_id         TEXT        PRIMARY KEY,
    repo_node_id    TEXT        NOT NULL REFERENCES repositories(node_id) ON DELETE CASCADE,
    number          INTEGER     NOT NULL,
    title           TEXT,
    state           TEXT,                             
    author_login    TEXT,
    created_at      TIMESTAMPTZ,
    merged_at       TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    commit_count    INTEGER,
    crawled_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (repo_node_id, number)
);


CREATE TABLE IF NOT EXISTS comments (
    node_id         TEXT        PRIMARY KEY,
    parent_node_id  TEXT        NOT NULL,             
    parent_type     TEXT        NOT NULL              
        CHECK (parent_type IN ('issue', 'pull_request')),
    author_login    TEXT,
    body_length     INTEGER,                        
    created_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ,
    crawled_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_node_id, parent_type);

CREATE TABLE IF NOT EXISTS pr_reviews (
    node_id         TEXT        PRIMARY KEY,
    pr_node_id      TEXT        NOT NULL REFERENCES pull_requests(node_id) ON DELETE CASCADE,
    author_login    TEXT,
    state           TEXT,                             
    submitted_at    TIMESTAMPTZ,
    crawled_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS ci_checks (
    node_id         TEXT        PRIMARY KEY,
    pr_node_id      TEXT        REFERENCES pull_requests(node_id) ON DELETE CASCADE,
    repo_node_id    TEXT        NOT NULL REFERENCES repositories(node_id) ON DELETE CASCADE,
    name            TEXT,
    status          TEXT,                           
    conclusion      TEXT,                             
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    crawled_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);