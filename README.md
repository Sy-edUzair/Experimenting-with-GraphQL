# GitHub Repository Stars Crawler

A high-performance GitHub repository crawler that collects star counts for **100,000 repositories in ~8 minutes** using GitHub's GraphQL API, stores results in PostgreSQL, and runs as a fully automated daily GitHub Actions pipeline.

---

## Performance

| Metric | Value |
|---|---|
| Target repositories | 100,000 |
| Crawl duration | ~8 minutes |
| Throughput | ~260 repos/second |
| Concurrency | 20 simultaneous GraphQL queries |
| Query space | 1,760+ unique search combinations |

---

## Architecture

The project follows **Clean Architecture** principles with three distinct layers. Dependencies always point inward — infrastructure depends on application, application depends on domain, domain depends on nothing.

```
┌─────────────────────────────────────────────┐
│                   main.py                   │
│         (Composition Root — wires           │
│          all dependencies together)         │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│            Application Layer                │
│  CrawlApplicationService  (use case)        │
│  CrawlerOrchestrator      (concurrency)     │
│  MultiDimensionalQueryGenerator             │
│  InMemoryDeduplicator                       │
└──────────────────┬──────────────────────────┘
                   │
     ┌─────────────┼─────────────┐
     ▼             ▼             ▼
┌─────────┐  ┌─────────┐  ┌──────────────────┐
│ Domain  │  │ Infra   │  │    Infra         │
│ Layer   │  │ GitHub  │  │  PostgreSQL      │
│entities │  │ Client  │  │  Storage         │
│interfcs │  │         │  │                  │
└─────────┘  └─────────┘  └──────────────────┘
```

### Folder Structure

```
sql/
  └── schema.sql                   # PostgreSQL schema
scripts
│
├── main.py                          # Dependency wiring only — no logic
├── dump_db.py                       # Export results to CSV
│
│
├── src/
│   ├── domain/                      # Innermost layer — zero external dependencies
│   │   ├── entities.py              # GitHubRepo, CrawlResult (immutable dataclasses)
│   │   └── interfaces.py            # IRepoFetcher, IRepoStorage, IQueryGenerator, IDeduplicator
│   │
│   ├── application/                 # Business logic — depends only on domain
│   │   ├── crawl_service.py         # Top-level use case orchestration
│   │   ├── orchestrator.py          # Async concurrency management
│   │   ├── deduplicator.py          # Repo deduplication (separated concern)
│   │   └── query_generator.py       # Multi-dimensional query generation
│   │
│   └── infrastructure/              # Outermost layer — talks to external systems
│       ├── github_client.py         # GitHub GraphQL API + anti-corruption layer
│       └── postgres_storage.py      # PostgreSQL persistence
│
.github/
    └── workflows/
        └── crawl.yml                # Daily automated pipeline
```

---

## Key Engineering Decisions

### 1. Anti-Corruption Layer

GitHub's API returns camelCase field names (`stargazerCount`, `nameWithOwner`). The anti-corruption layer in `github_client.py` translates these into our own clean domain model before any other code touches the data.

```python
# GitHub sends this:
{ "nameWithOwner": "torvalds/linux", "stargazerCount": 185000 }

# _parse_node() translates it to our domain model:
GitHubRepo(name_with_owner="torvalds/linux", star_count=185000)
```

If GitHub renames a field tomorrow, **only one file changes** — `github_client.py`.

### 2. Immutable Domain Objects

All domain entities use `frozen=True` dataclasses. Once created, no field can be modified.

```python
@dataclass(frozen=True)
class GitHubRepo:
    node_id:    str
    star_count: int
    ...
```

Data flows one way: API → domain object → database. Accidental mutation is impossible.

### 3. Dependency Injection

No class creates its own dependencies. Everything is injected from `main.py`.

```python
# CrawlerOrchestrator receives these — it creates NONE of them
orchestrator = CrawlerOrchestrator(
    fetcher      = github_client,    # IRepoFetcher
    generator    = query_generator,  # IQueryGenerator
    deduplicator = deduplicator,     # IDeduplicator
)
```

This means every class is independently testable with fake/mock implementations.

### 4. Async Concurrency — 20 Simultaneous Queries

The single biggest performance improvement. Instead of waiting for each query to finish before starting the next, 20 queries run simultaneously using `asyncio` + `httpx.AsyncClient`.

```
Sequential (old):  [query 1]──[query 2]──[query 3]──[query 4]...  83 min
Concurrent (new):  [query 1 ]
                   [query 2 ]   all finish around the same time    ~8 min
                   [query 3 ]
                   ... ×20
```

`asyncio.Semaphore(20)` acts as a gate — at most 20 queries in flight at once.

### 5. Multi-Dimensional Query Generation

GitHub's Search API returns at most 1,000 results per query. To reach 100,000 repositories, queries are split across three dimensions:

```
language × star_range × creation_year

20 languages × 8 star ranges × 10 years = 1,600 queries
+ 20 × 8 fallback (no year filter)      =   160 queries
Total: 1,760 queries × 1,000 max each   = 1,760,000 potential repos
```

Each combination is a unique, non-overlapping search — no duplicates, full coverage.

### 6. JSONB Flexible Schema

The `extra JSONB` column stores metadata that doesn't need a dedicated column. New fields can be added tomorrow with zero database migrations — just add a key to the JSON dict.

```sql
-- Current extra column contains:
{ "description": "...", "primary_language": "Python", "is_private": false }

-- Tomorrow add forks, topics, license — zero ALTER TABLE needed
{ "description": "...", "primary_language": "Python", "forks_count": 5000 }
```

### 7. Separation of Concerns

Each class has exactly one job:

| Class | Single Responsibility |
|---|---|
| `GitHubClient` | Communicate with GitHub API |
| `PostgresRepoStorage` | Persist data to PostgreSQL |
| `MultiDimensionalQueryGenerator` | Generate search query strings |
| `InMemoryDeduplicator` | Track and filter duplicate repos |
| `CrawlerOrchestrator` | Manage async concurrency |
| `CrawlApplicationService` | Sequence the full crawl use case |
| `main.py` | Wire dependencies together |

---

## Database Schema

```sql
repositories (
    node_id     TEXT PRIMARY KEY,      -- GitHub's stable global ID
    full_name   TEXT UNIQUE,           -- "owner/repo" format
    name        TEXT,
    owner_login TEXT,
    stars       INTEGER,               -- current star count
    scraped_at  TIMESTAMPTZ,           -- when we last crawled it
    extra       JSONB                  -- flexible metadata (language, description, etc.)
)

crawl_runs (
    id          SERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    total_repos INTEGER,
    status      TEXT,                  -- 'running' | 'success' | 'failed'
    error_msg   TEXT
)
```

The `repos_view` view exposes JSONB fields as typed columns for easy querying:

```sql
SELECT full_name, stars, primary_language
FROM repos_view
WHERE primary_language = 'Python'
ORDER BY stars DESC;
```

---

## GitHub Actions Pipeline

The pipeline runs automatically every day at **02:00 UTC (07:00 AM Karachi)** and can also be triggered manually.

```
Step 1: Start PostgreSQL service container
Step 2: Checkout code
Step 3: Set up Python 3.12
Step 4: Install dependencies (httpx, psycopg2-binary)
Step 5: Apply database schema
Step 6: Crawl 100,000 repos via GitHub GraphQL API    ← ~8 minutes
Step 7: Export results to CSV
Step 8: Upload CSV as downloadable artifact (kept 30 days)
Step 9: Upload full DB dump as artifact
```

No secrets or elevated permissions required — uses the default `GITHUB_TOKEN` automatically provided by GitHub Actions.

---

## Local Setup

### Prerequisites

- Python 3.12+
- Docker Desktop
- Git

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/github-crawler.git
cd github-crawler
```

### 2. Install dependencies

```bash
pip install httpx psycopg2-binary
```

### 3. Start PostgreSQL

```bash
docker run -d \
  --name gh-crawler-pg \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=github_crawler \
  -p 5433:5432 \
  postgres:16
```

### 4. Apply schema

```bash
docker exec -i gh-crawler-pg psql -U postgres -d github_crawler < sql/schema.sql
```

### 5. Set environment variables

```bash
# Mac/Linux
export DATABASE_URL="postgresql://postgres:postgres@localhost:5433/github_crawler"
export GITHUB_TOKEN="ghp_your_token_here"

# Windows (Command Prompt)
set DATABASE_URL=postgresql://postgres:postgres@localhost:5433/github_crawler
set GITHUB_TOKEN=ghp_your_token_here
```

### 6. Run a quick test (500 repos)

```bash
python main.py --target 500
```

### 7. Run the full crawl

```bash
python main.py --target 100000
```

### 8. Export results to CSV

```bash
python dump_db.py
```

---
## Answers to Questions:
The answers to question on how to scale schema for 500M repos is in the file "Answers to scale schema.pdf"

## Querying the Data

**Top 10 repos by stars:**
```sql
SELECT full_name, stars
FROM repos_view
ORDER BY stars DESC
LIMIT 10;
```

**Most popular Python repos:**
```sql
SELECT full_name, stars
FROM repos_view
WHERE primary_language = 'Python'
ORDER BY stars DESC
LIMIT 20;
```

**Crawl history:**
```sql
SELECT id, status, total_repos, started_at, finished_at
FROM crawl_runs
ORDER BY id DESC;
```

