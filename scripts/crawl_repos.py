"""
GitHub Stars Crawler
--------------------
Fetches 100,000 GitHub repositories and their star counts using the
GraphQL Search API, then upserts the results into PostgreSQL.

Design principles:
  - Anti-corruption layer: GitHubRepo dataclass isolates API shape from DB shape
  - Immutable data transfer: dataclasses with frozen=True
  - Separation of concerns: fetcher / transformer / repository layers
  - Respects rate limits with exponential backoff
  - Concurrent page fetching within each rate-limit window
"""

from __future__ import annotations

import os
import sys
import time
import logging
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

import httpx
import psycopg2
from psycopg2.extras import execute_values

GITHUB_API_URL = "https://api.github.com/graphql"
# GitHub GraphQL Search allows max 100 results per page
PAGE_SIZE = 100
TARGET_REPOS = 100_000
MAX_CONCURRENT = 5
RATE_LIMIT_SLEEP = 60
MAX_RETRIES = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Anti-Corruption Layer: domain model (immutable)
@dataclass(frozen=True)
class GitHubRepo:
    """Domain model — decoupled from GitHub API response shape."""
    node_id: str
    name_with_owner: str
    name: str
    owner_login: str
    description: str | None
    primary_language: str | None
    is_private: bool
    star_count: int
    created_at: datetime | None
    updated_at: datetime | None


# We search across multiple "buckets" of star ranges to bypass the 1000-result limit imposed by GitHub's Search API (it only returns the first 1000 results for any single query). By splitting into star-range buckets we can cover far more repositories.
def _build_star_buckets() -> list[str]:
    buckets = []

    # Very high stars: broad ranges are fine (few repos, well under 1k each)
    buckets += [
        "stars:>100000",
        "stars:50001..100000",
    ]

    # 10k–50k: split into bands of ~5k
    for lo in range(10000, 50001, 5000):
        hi = lo + 4999
        buckets.append(f"stars:{lo}..{hi}")

    # 5k–10k: bands of 1k
    for lo in range(5000, 10000, 1000):
        hi = lo + 999
        buckets.append(f"stars:{lo}..{hi}")

    # 1k–5k: bands of 500
    for lo in range(1000, 5000, 500):
        hi = lo + 499
        buckets.append(f"stars:{lo}..{hi}")

    # 500–999: bands of 100
    for lo in range(500, 1000, 100):
        hi = lo + 99
        buckets.append(f"stars:{lo}..{hi}")

    # 100–499: bands of 50
    for lo in range(100, 500, 50):
        hi = lo + 49
        buckets.append(f"stars:{lo}..{hi}")

    # 10–99: individual star counts (each is its own bucket → 1,000 repos each)
    for n in range(99, 9, -1):
        buckets.append(f"stars:{n}")

    # 1–9: individual counts
    for n in range(9, 0, -1):
        buckets.append(f"stars:{n}")

    # Zero stars
    buckets.append("stars:0")

    return buckets


STAR_BUCKETS = _build_star_buckets()

GRAPHQL_QUERY = """
query SearchRepos($query: String!, $first: Int!, $after: String) {
  rateLimit {
    remaining
    resetAt
    cost
  }
  search(query: $query, type: REPOSITORY, first: $first, after: $after) {
    repositoryCount
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on Repository {
        id
        nameWithOwner
        name
        owner { login }
        description
        primaryLanguage { name }
        isPrivate
        stargazerCount
        createdAt
        updatedAt
      }
    }
  }
}
"""


class RateLimitError(Exception):
    """Raised when GitHub rate limit is exhausted."""
    def __init__(self, reset_at: str):
        self.reset_at = reset_at
        super().__init__(f"Rate limit exhausted, resets at {reset_at}")


# GitHub API fetcher
class GitHubFetcher:
    """Handles all communication with GitHub GraphQL API."""

    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Github-Next-Global-ID": "1",  # opt-in to stable global IDs
        }

    def _parse_repo(self, node: dict) -> GitHubRepo | None:
        """Transform raw API node → domain object (anti-corruption layer)."""
        try:
            return GitHubRepo(
                node_id=node["id"],
                name_with_owner=node["nameWithOwner"],
                name=node["name"],
                owner_login=node["owner"]["login"],
                description=node.get("description"),
                primary_language=(
                    node["primaryLanguage"]["name"]
                    if node.get("primaryLanguage")
                    else None
                ),
                is_private=node.get("isPrivate", False),
                star_count=node.get("stargazerCount", 0),
                created_at=self._parse_dt(node.get("createdAt")),
                updated_at=self._parse_dt(node.get("updatedAt")),
            )
        except (KeyError, TypeError) as exc:
            log.warning("Skipping malformed node: %s — %s", node.get("id"), exc)
            return None

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def fetch_page(self,star_query: str,cursor: str | None = None,) -> tuple[list[GitHubRepo], bool, str | None]:
        """
        Fetch one page of results for a star-range query.
        Returns: (repos, has_next_page, end_cursor)
        """
        variables = {"query": star_query, "first": PAGE_SIZE, "after": cursor}
        for attempt in range(MAX_RETRIES):
            try:
                resp = httpx.post(
                    GITHUB_API_URL,
                    headers=self._headers,
                    json={"query": GRAPHQL_QUERY, "variables": variables},
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()

                if "errors" in data:
                    for err in data["errors"]:
                        if err.get("type") == "RATE_LIMITED":
                            reset_at = (
                                data.get("data", {})
                                .get("rateLimit", {})
                                .get("resetAt", "unknown")
                            )
                            raise RateLimitError(reset_at)
                    log.warning("GraphQL errors: %s", data["errors"])

                rate = data["data"]["rateLimit"]
                log.debug(
                    "Rate limit: %d remaining (cost %d), resets %s",
                    rate["remaining"],
                    rate["cost"],
                    rate["resetAt"],
                )

                # Proactively sleep when approaching limit
                if rate["remaining"] < 10:
                    log.info(
                        "Rate limit low (%d remaining). Sleeping %ds …",
                        rate["remaining"],
                        RATE_LIMIT_SLEEP,
                    )
                    time.sleep(RATE_LIMIT_SLEEP)

                search = data["data"]["search"]
                repos = [
                    r
                    for node in search["nodes"]
                    if (r := self._parse_repo(node)) is not None
                ]
                page_info = search["pageInfo"]
                return repos, page_info["hasNextPage"], page_info["endCursor"]

            except RateLimitError as exc:
                log.info("Rate limit hit. Sleeping %ds …", RATE_LIMIT_SLEEP)
                time.sleep(RATE_LIMIT_SLEEP)

            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                wait = 2 ** attempt
                log.warning(
                    "Request error (attempt %d/%d): %s. Retrying in %ds …",
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)

        raise RuntimeError(f"Failed to fetch page after {MAX_RETRIES} retries")

    def iter_repos(self, target: int) -> Iterator[list[GitHubRepo]]:
        """
        Yields batches of GitHubRepo, cycling through star buckets until
        `target` unique repositories have been collected.
        """
        seen_ids: set[str] = set()
        total = 0

        for bucket in STAR_BUCKETS:
            if total >= target:
                break

            cursor = None
            bucket_count = 0
            log.info("Starting bucket: %s", bucket)

            while total < target:
                repos, has_next, cursor = self.fetch_page(bucket, cursor)

                # De-duplicate across buckets
                fresh = [r for r in repos if r.node_id not in seen_ids]
                for r in fresh:
                    seen_ids.add(r.node_id)

                total += len(fresh)
                bucket_count += len(fresh)

                if fresh:
                    yield fresh

                log.info(
                    "Bucket %-25s | page done | bucket=%d | total=%d/%d",
                    bucket,
                    bucket_count,
                    total,
                    target,
                )

                if not has_next or not fresh:
                    break

        log.info("Collection complete: %d unique repos", total)

# Database repository (persistence layer)
class RepoRepository:
    """All database interactions. No business logic here."""

    def __init__(self, conn):
        self._conn = conn

    def upsert_batch(self, repos: list[GitHubRepo], crawl_run_id: int) -> None:
        """
        Upsert repositories and insert new star-count snapshots.
        Star counts are append-only (one row per day per repo), so only
        NEW rows are written — existing history is never modified.
        """
        now = datetime.now(tz=timezone.utc)

        repo_rows = [
            (
                r.node_id,
                r.name_with_owner,
                r.name,
                r.owner_login,
                r.description,
                r.primary_language,
                r.is_private,
                r.created_at,
                r.updated_at,
                now,
            )
            for r in repos
        ]

        star_rows = [
            (r.node_id, r.star_count, now)
            for r in repos
        ]

        with self._conn.cursor() as cur:
            # Upsert repos - only update mutable fields
            execute_values(
                cur,
                """
                INSERT INTO repositories
                    (node_id, name_with_owner, name, owner_login, description,
                     primary_language, is_private, created_at, updated_at, crawled_at)
                VALUES %s
                ON CONFLICT (node_id) DO UPDATE SET
                    name_with_owner  = EXCLUDED.name_with_owner,
                    name             = EXCLUDED.name,
                    owner_login      = EXCLUDED.owner_login,
                    description      = EXCLUDED.description,
                    primary_language = EXCLUDED.primary_language,
                    is_private       = EXCLUDED.is_private,
                    updated_at       = EXCLUDED.updated_at,
                    crawled_at       = EXCLUDED.crawled_at
                """,
                repo_rows,
            )

            # Insert star snapshots — append only, no updates
            execute_values(
                cur,
                """
                INSERT INTO repository_stars (node_id, star_count, recorded_at)
                VALUES %s
                ON CONFLICT DO NOTHING
                """,
                star_rows,
            )

        self._conn.commit()

    def update_crawl_run(self,run_id: int,repos_fetched: int,status: str,error_message: str | None = None) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crawl_runs
                SET finished_at    = NOW(),
                    repos_fetched  = %s,
                    status         = %s,
                    error_message  = %s
                WHERE id = %s
                """,
                (repos_fetched, status, error_message, run_id),
            )
        self._conn.commit()

    def create_crawl_run(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crawl_runs (started_at) VALUES (NOW()) RETURNING id"
            )
            run_id = cur.fetchone()[0]
        self._conn.commit()
        return run_id

# Orchestrator
def run_crawl(db_url: str, github_token: str, target: int = TARGET_REPOS) -> None:
    log.info("Connecting to database …")
    conn = psycopg2.connect(db_url)

    repo_repository = RepoRepository(conn)
    run_id = repo_repository.create_crawl_run()
    log.info("Crawl run #%d started", run_id)

    fetcher = GitHubFetcher(github_token)
    total_fetched = 0

    try:
        for batch in fetcher.iter_repos(target):
            repo_repository.upsert_batch(batch, run_id)
            total_fetched += len(batch)
            log.info("Persisted %d repos (total so far: %d)", len(batch), total_fetched)

        repo_repository.update_crawl_run(run_id, total_fetched, "success")
        log.info("Crawl complete. %d repos stored.", total_fetched)

    except Exception as exc:
        log.error("Crawl failed: %s", exc, exc_info=True)
        repo_repository.update_crawl_run(run_id, total_fetched, "failed", str(exc))
        conn.close()
        sys.exit(1)

    conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GitHub repository stars crawler")
    parser.add_argument(
        "--target",
        type=int,
        default=TARGET_REPOS,
        help=f"Number of repos to collect (default: {TARGET_REPOS})",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    print(db_url)
    if not db_url:
        log.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        log.error("GITHUB_TOKEN environment variable is required")
        sys.exit(1)

    run_crawl(db_url, github_token, args.target)