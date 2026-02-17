from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.domain.entities import CrawlResult
from src.domain.interfaces import IRepoStorage
from .orchestrator import CrawlerOrchestrator

log = logging.getLogger(__name__)


class CrawlApplicationService:
    """
    The top-level use case: crawl GitHub and persist results.

    Receives all dependencies via constructor injection.
    Knows about the sequence of operations but not the implementation details.
    """

    def __init__(self,orchestrator: CrawlerOrchestrator, storage:IRepoStorage,) -> None:
        self._orchestrator = orchestrator
        self._storage      = storage

    async def execute(self, target: int) -> CrawlResult:
        """
        Run a full crawl for `target` repositories.
        Returns a CrawlResult describing what happened.
        """
        started_at = datetime.now(tz=timezone.utc)
        run_id     = self._storage.create_run()
        total      = 0

        log.info("CrawlApplicationService | run #%d | target: %d", run_id, target)

        try:
            async for batch in self._orchestrator.collect(target):
                # Trim batch if it would push us past the target
                remaining = target - total
                if len(batch) > remaining:
                    batch = batch[:remaining]

                self._storage.upsert_batch(batch)
                total += len(batch)

                elapsed = (datetime.now(tz=timezone.utc) - started_at).total_seconds()
                rate    = total / elapsed if elapsed > 0 else 0
                log.info("Saved %d repos | running total: %d/%d | %.1f repos/sec",len(batch), total, target, rate)
                if total >= target:
                    break

            elapsed = (datetime.now(tz=timezone.utc) - started_at).total_seconds()
            self._storage.finish_run(run_id, total, "success")
            log.info("Crawl complete | %d repos | %.0fs | %.1f repos/sec",total, elapsed, total / elapsed if elapsed > 0 else 0)
            return CrawlResult(
                run_id       = run_id,
                total_repos  = total,
                status       = "success",
                elapsed_secs = elapsed,
            )
        except Exception as exc:
            elapsed = (datetime.now(tz=timezone.utc) - started_at).total_seconds()
            log.error("Crawl failed: %s", exc, exc_info=True)
            self._storage.finish_run(run_id, total, "failed", str(exc))

            return CrawlResult(
                run_id        = run_id,
                total_repos   = total,
                status        = "failed",
                elapsed_secs  = elapsed,
                error_message = str(exc),
            )