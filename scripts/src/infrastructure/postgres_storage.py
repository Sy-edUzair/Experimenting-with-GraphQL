from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from psycopg2.extras import execute_values
from src.domain.entities import GitHubRepo
from src.domain.interfaces import IRepoStorage

log = logging.getLogger(__name__)


class PostgresRepoStorage(IRepoStorage):
    """
    Concrete implementation of IRepoStorage using PostgreSQL.

    Receives an already-connected psycopg2 connection (injected).
    Does not create or manage the connection itself — that's the
    responsibility of the caller (main.py / dependency wiring).
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def upsert_batch(self, repos: list[GitHubRepo]) -> None:
        """
        Insert or update a batch of repos in a single SQL statement.

        ON CONFLICT (node_id) DO UPDATE means:
          - New repo    → INSERT
          - Existing repo → UPDATE only the columns that can change
            (stars, scraped_at, extra). node_id and full_name are stable.

        execute_values sends all rows in ONE round-trip to the DB
        instead of N separate INSERT statements — much faster at scale.
        """
        now = datetime.now(tz=timezone.utc)

        rows = [
            (
                r.node_id,
                r.name_with_owner,
                r.name,
                r.owner_login,
                r.star_count,
                now,
                # JSONB extra: all fields that don't have dedicated columns
                # Adding new fields = just add a key here, zero DB migration
                json.dumps({
                    "description":      r.description,
                    "primary_language": r.primary_language,
                    "is_private":       r.is_private,
                    "created_at":       r.created_at.isoformat() if r.created_at else None,
                    "updated_at":       r.updated_at.isoformat() if r.updated_at else None,
                }),
            )
            for r in repos
        ]

        with self._conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO repositories
                    (node_id, full_name, name, owner_login, stars, scraped_at, extra)
                VALUES %s
                ON CONFLICT (node_id) DO UPDATE SET
                    full_name  = EXCLUDED.full_name,
                    stars      = EXCLUDED.stars,
                    scraped_at = EXCLUDED.scraped_at,
                    extra      = EXCLUDED.extra
                """,
                rows,
            )
        self._conn.commit()
        log.debug("Upserted %d repos to PostgreSQL", len(repos))

    def create_run(self) -> int:
        """
        Create a crawl_runs row when the crawl starts.
        Returns the new run ID so we can update it when finished.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO crawl_runs (started_at, status)
                VALUES (NOW(), 'running')
                RETURNING id
                """
            )
            run_id = cur.fetchone()[0]
        self._conn.commit()
        log.debug("Created crawl run #%d", run_id)
        return run_id

    def finish_run(self,run_id: int, total: int, status: str, error:str | None = None) -> None:
        """
        Update the crawl_runs row with final stats.
        Called on both success and failure.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crawl_runs
                SET finished_at = NOW(),
                    total_repos = %s,
                    status      = %s,
                    error_msg   = %s
                WHERE id = %s
                """,
                (total, status, error, run_id),
            )
        self._conn.commit()
        log.debug("Finished crawl run #%d | status=%s | total=%d", run_id, status, total)