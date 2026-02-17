"""
main.py — Dependency Wiring (Composition Root)
------------------------------------------------
This file has ONE job: wire all the pieces together and run the app.

It does NOT contain any business logic. It just:
  1. Reads configuration from environment variables
  2. Creates concrete implementations of each interface
  3. Injects them into the classes that need them
  4. Calls the top-level use case (CrawlApplicationService.execute)
  5. Reports the result and exits

This pattern is called the "Composition Root" — the single place in the
application where all dependencies are wired together. Every other class
receives its dependencies via constructor injection rather than creating
them, which makes every class independently testable.

Dependency graph (what depends on what):
                         main.py  (wires everything)
                            │
              ┌─────────────┼──────────────┐
              ▼             ▼              ▼
    CrawlApplicationService │    PostgresRepoStorage
              │             │
              ▼             ▼
    CrawlerOrchestrator  GitHubClient
              │
    ┌─────────┼──────────┐
    ▼         ▼          ▼
IRepoFetcher  IQueryGenerator  IDeduplicator
(GitHub)      (MultiDim)       (InMemory)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import argparse

import httpx
import psycopg2

# Application layer
from src.application.crawl_service import CrawlApplicationService
from src.application.orchestrator import CrawlerOrchestrator
from src.application.query_generator import MultiDimensionalQueryGenerator
from src.application.deduplicator import InMemoryDeduplicator

# Infrastructure layer
from src.infrastructure.github_client import GitHubClient
from src.infrastructure.postgres_storage import PostgresRepoStorage

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TARGET = 100_000


def _read_env() -> tuple[str, str]:
    """
    Read required environment variables.
    Fails fast with a clear error if either is missing.
    """
    db_url = os.environ.get("DATABASE_URL")
    token  = os.environ.get("GITHUB_TOKEN")

    if not db_url:
        log.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    if not token:
        log.error("GITHUB_TOKEN environment variable is required")
        sys.exit(1)

    return db_url, token


# ---------------------------------------------------------------------------
# Dependency wiring
# ---------------------------------------------------------------------------

async def build_and_run(db_url: str, token: str, target: int) -> None:
    """
    Wires all dependencies together and executes the crawl use case.

    This is the Composition Root — the only place that knows which
    concrete class implements each interface.

    To swap implementations (e.g. use a different DB):
      Change ONE line here. Nothing else in the codebase changes.
    """

    # Infrastructure: create the DB connection and HTTP client
    conn   = psycopg2.connect(db_url)
    client = httpx.AsyncClient()

    try:
        # --- Wire the dependency graph bottom-up ---

        # Infrastructure implementations
        github_client = GitHubClient(
            token  = token,
            client = client,       # injected — GitHubClient doesn't create this
        )
        storage = PostgresRepoStorage(
            conn = conn,           # injected — storage doesn't create the connection
        )

        # Application services (receive infrastructure via injection)
        query_generator = MultiDimensionalQueryGenerator()
        deduplicator    = InMemoryDeduplicator()
        orchestrator    = CrawlerOrchestrator(
            fetcher       = github_client,    # injected IRepoFetcher
            generator     = query_generator,  # injected IQueryGenerator
            deduplicator  = deduplicator,     # injected IDeduplicator
        )

        # Top-level use case (receives application services via injection)
        crawl_service = CrawlApplicationService(
            orchestrator = orchestrator,  # injected
            storage      = storage,       # injected IRepoStorage
        )

        # --- Execute ---
        result = await crawl_service.execute(target)

        # --- Report ---
        if result.status == "success":
            log.info(
                "✅ Success | %d repos | %.0fs | run_id=%d",
                result.total_repos,
                result.elapsed_secs,
                result.run_id,
            )
        else:
            log.error(
                "❌ Failed | %d repos collected before failure | error: %s",
                result.total_repos,
                result.error_message,
            )
            sys.exit(1)

    finally:
        # Always clean up connections, even if an exception occurred
        await client.aclose()
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="High-performance GitHub repository stars crawler"
    )
    parser.add_argument(
        "--target",
        type    = int,
        default = DEFAULT_TARGET,
        help    = f"Number of repos to collect (default: {DEFAULT_TARGET})",
    )
    args = parser.parse_args()

    db_url, token = _read_env()

    asyncio.run(build_and_run(db_url, token, args.target))