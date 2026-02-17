from __future__ import annotations
import asyncio
import logging
from typing import AsyncIterator
import httpx
from src.domain.entities import GitHubRepo
from src.domain.interfaces import IRepoFetcher, IQueryGenerator, IDeduplicator

log = logging.getLogger(__name__)

MAX_CONCURRENT= 15    
RATE_LIMIT_SLEEP = 60  


class CrawlerOrchestrator:
    """
    Coordinates concurrent crawling using asyncio.

    All dependencies are injected — this class creates NOTHING itself:
      - IRepoFetcher     → how to talk to GitHub (injected)
      - IQueryGenerator  → what queries to run (injected)
      - IDeduplicator    → how to deduplicate (injected)

    This means in tests you can pass:
      FakeGitHubFetcher, StaticQueryGenerator, InMemoryDeduplicator
    ...and test the orchestration logic without any real network/DB calls.
    """

    def __init__(self,fetcher:IRepoFetcher,generator:IQueryGenerator,deduplicator:IDeduplicator,max_concurrent: int = MAX_CONCURRENT) -> None:
        self._fetcher      = fetcher
        self._generator    = generator
        self._deduplicator = deduplicator
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent

    async def _run_single_query(self,client:httpx.AsyncClient,query_str:str,target:int,out:list[GitHubRepo],stop_event: asyncio.Event) -> int:
        """
        Fetch all pages for one query string.
        Returns count of fresh repos found.

        Uses stop_event instead of checking a shared counter directly —
        cleaner signal mechanism between coroutines.
        """
        cursor = None
        found  = 0

        while not stop_event.is_set():
            async with self._semaphore:
                try:
                    repos, has_next, cursor, rate = await self._fetcher.fetch_page(
                        query_str, cursor
                    )
                except RuntimeError as exc:
                    log.warning("Query failed, skipping: %.60s | %s", query_str, exc)
                    return found    # log and skip — don't crash the whole crawl

            fresh = await self._deduplicator.filter_fresh_async(repos)
            out.extend(fresh)
            found += len(fresh)

            if rate < 20:
                log.info("Rate limit low (%d remaining) — pausing %ds …", rate, RATE_LIMIT_SLEEP)
                await asyncio.sleep(RATE_LIMIT_SLEEP)

            if not has_next or not repos:
                break

            if self._deduplicator.total_seen() >= target:
                stop_event.set()    # signal all other coroutines to stop
                break

        return found

    async def collect(self, target: int) -> AsyncIterator[list[GitHubRepo]]:
        """
        Async generator — yields batches of fresh repos as they arrive.

        Processes queries in chunks of (MAX_CONCURRENT × 4).
        Within each chunk, all queries run simultaneously via asyncio.gather.

        Why async generator instead of returning all at once?
        - Memory: never holds all 100k repos in RAM simultaneously
        - Progress: caller sees repos arriving in real time
        - Resilience: if it crashes at 80k, you've already saved 80k
        """
        queries    = self._generator.generate()
        chunk_size = self._max_concurrent * 4
        stop_event = asyncio.Event()

        log.info("Starting crawl | queries=%d | concurrency=%d | target=%d",len(queries), self._max_concurrent, target)

        async with httpx.AsyncClient() as client:
            for i in range(0, len(queries), chunk_size):
                if stop_event.is_set() or self._deduplicator.total_seen() >= target:
                    break

                chunk = queries[i: i + chunk_size]
                batch: list[GitHubRepo] = []

                # Launch entire chunk simultaneously — this is the speed trick
                await asyncio.gather(*[self._run_single_query(client, q, target, batch, stop_event)for q in chunk ])

                if batch:
                    log.info(
                        "Chunk %d/%d | +%d new | total %d/%d",
                        i // chunk_size + 1,
                        (len(queries) + chunk_size - 1) // chunk_size,
                        len(batch),
                        self._deduplicator.total_seen(),
                        target,
                    )
                    yield batch

        log.info("Crawl complete - total unique repos: %d", self._deduplicator.total_seen())