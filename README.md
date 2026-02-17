GitHub Repository Stars Crawler
A production-grade GitHub crawler that collects star counts for 100,000 repositories using GitHub's GraphQL API, stores results in PostgreSQL, and runs as a daily GitHub Actions pipeline.

Architecture Overview
GitHub GraphQL API
        │
        ▼
  GitHubFetcher            ← Transport layer (HTTP, rate-limit handling, retries)
        │
        ▼ (anti-corruption layer)
   GitHubRepo              ← Immutable domain model (decoupled from API shape)
        │
        ▼
 RepoRepository            ← Persistence layer (pure DB operations)
        │
        ▼
   PostgreSQL              ← Source of truth
Design Principles Applied
PrincipleImplementationAnti-corruption layerGitHubFetcher._parse_repo() translates raw API JSON → GitHubRepo dataclass. DB schema is entirely independent of API field names.ImmutabilityGitHubRepo is a frozen=True dataclass. Data flows one-way from fetcher → domain object → DB.Separation of concernsThree distinct layers: fetcher (transport), domain (data model), repository (persistence). No business logic bleeds into the DB layer.Efficient updatesStar counts are append-only — new row per day per repo. Upsert on repositories only changes mutable fields. A PR gaining 10 more comments tomorrow = 10 new rows in comments, not a full re-scan.

How It Works
Bypassing the 1,000-result API limit
GitHub's Search API returns at most 1,000 results per query. To collect 100,000 repositories, the crawler partitions the search space into star-count buckets (e.g. stars:>50000, stars:10000..50000, etc.). Each bucket yields up to 1,000 results, and cycling through ~14 buckets easily provides 100,000+ unique repos.
Rate Limit Handling

Every GraphQL response includes rateLimit.remaining. When this drops below 10, the crawler proactively sleeps for 60 seconds.
Explicit RATE_LIMITED errors trigger the same sleep.
Transient HTTP errors use exponential backoff (2ˢ seconds on attempt s).
Maximum 5 retries per page before failing the run.

GitHub Actions Pipeline Steps

postgres service — starts a PostgreSQL 16 container
setup-python — installs Python 3.12
install dependencies — pip install httpx psycopg2-binary
setup-postgres — applies sql/schema.sql (creates all tables/views)
crawl-stars — runs scripts/crawl_stars.py; uses ${{ secrets.GITHUB_TOKEN }} (default token, no extra permissions needed)
dump-db — exports latest_star_counts view → star_counts.csv
upload-artifact — uploads CSV; retained 30 days


Database Schema
Core Tables
sqlrepositories        -- one row per repo (stable pk = GitHub node_id)
repository_stars    -- one row per (repo, day) — append-only star snapshots
crawl_runs          -- audit trail for every pipeline execution
Extension Tables (already defined, populated when needed)
sqlissues              pull_requests       comments
pr_reviews          ci_checks
Key Design Decisions
Why use node_id as PK instead of id or name_with_owner?
GitHub's node_id is a stable global identifier that survives repository renames, transfers, and forks. Using it means a renamed repo (old/name → new/name) stays as the same row — no dangling references.
Why separate repository_stars table?
Storing star counts separately enables:

Time-series analysis (track star velocity over time)
Efficient daily updates (insert one new row, never touch historical data)
The latest_star_counts view abstracts this complexity from consumers


Scaling to 500 Million Repositories
If this pipeline needed to collect data on 500 million repositories instead of 100,000, here is what would change:
1. Parallelism & Sharding
The star-bucket approach works but would need to be dramatically expanded. With 500M repos, you'd shard the crawl by:

Date ranges (created:2020-01-01..2020-06-30), combined with star ranges
Language filters (language:Python stars:0..5)
Owner type (user:, org:)

Each shard would be an independent worker, running in parallel across many machines (e.g., Kubernetes Jobs or AWS ECS tasks).
2. Message Queue Architecture
Instead of one sequential script, use a message queue (Kafka, SQS, or RabbitMQ):

A scheduler publishes crawl tasks (one task = one GraphQL search query + cursor)
A fleet of worker processes picks up tasks, fetches pages, and publishes results to a results topic
A writer process consumes results and batch-upserts into the DB

This decouples rate-limit bottlenecks from throughput — if GitHub throttles one worker, others keep going.
3. Database: Partitioning & Read Replicas
At 500M repos:

Partition repositories by owner_login hash or creation year
Partition repository_stars by recorded_at (monthly or weekly range partitions), enabling fast pruning of old snapshots
Use Citus (distributed Postgres) or migrate to a horizontally scalable store like CockroachDB or BigQuery for analytics
Add read replicas to serve downstream consumers without hitting the writer

4. Incremental Crawling
Rather than re-crawling everything daily:

Use GitHub's Events API to detect repos that changed (pushes, stars, forks) since last crawl
Only re-fetch repos that have updated_at > last_crawled_at
Full re-crawl once a week; incremental updates every hour

5. Idempotency & Exactly-Once Processing
At scale, duplicate processing is inevitable. Ensure:

All upserts are idempotent (ON CONFLICT DO UPDATE)
Worker tasks carry a crawl_run_id and task_id so duplicates are detected and skipped
Use a distributed lock (Redis, DynamoDB) to prevent two workers claiming the same cursor

6. Observability

Emit metrics (Prometheus/CloudWatch) per worker: pages fetched, rate-limit hits, error rate
Alert on crawl lag (expected 500M repos / N workers = expected completion time)
Dead-letter queue for failed tasks with alerting


Schema Evolution for Richer Metadata
The schema is already designed with extension tables. Here is how each new data type would be handled efficiently:
Issues
sql-- issues table already defined
-- Upsert by node_id — only changed fields are updated
-- New issues = new rows; closed issues = UPDATE state = 'CLOSED'
One upsert per issue. If 1,000 new issues appear, 1,000 rows are written. Existing rows are untouched unless their state changed.
Pull Requests
Same pattern as issues. The pull_requests table stores one row per PR, upserted on node_id. Fields like merged_at and commit_count are updated in-place only when they change.
Comments (PRs and Issues)
Comments are a classic append-heavy pattern. The comments table uses node_id as PK. When a PR gains 10 new comments today and 20 more tomorrow:

Today: 10 INSERT rows
Tomorrow: 20 INSERT rows (new node_ids for new comments)
If an existing comment is edited: 1 UPDATE on its node_id (only that row is touched)

Zero full-table scans. The idx_comments_parent index makes "get all comments for PR X" fast.
Commits Inside Pull Requests
Add a commits table with (node_id PK, pr_node_id FK, sha, author_login, committed_at). Commits are immutable once merged, so this table is append-only after the PR closes.
Reviews on PRs
The pr_reviews table is already defined. One row per review, upserted on node_id. A reviewer who changes from CHANGES_REQUESTED to APPROVED = 1 UPDATE on their review row.
CI Checks
The ci_checks table supports status transitions (queued → in_progress → completed) via UPDATE on the existing row's conclusion field. A single CI run = 1 row updated, not replaced.
General Schema Evolution Strategy

Add columns as nullable — never break existing queries
New entity types = new tables — never add columns to repositories for unrelated data
Use node_id as FK everywhere — stable references across renames
Never delete data — soft deletes via deleted_at TIMESTAMPTZ column
Views as contracts — downstream consumers use views (latest_star_counts), so the underlying table structure can change without breaking them


Running Locally
bash# Start Postgres
docker run -d \
  --name gh-crawler-pg \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=github_crawler \
  -p 5432:5432 \
  postgres:16

# Apply schema
PGPASSWORD=postgres psql -h localhost -U postgres -d github_crawler -f sql/schema.sql

# Install deps
pip install httpx psycopg2-binary

# Run crawl (set your token)
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/github_crawler"
export GITHUB_TOKEN="ghp_your_token_here"
python scripts/crawl_stars.py --target 100000

# Dump results
python scripts/dump_db.py