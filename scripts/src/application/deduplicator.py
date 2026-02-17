from __future__ import annotations
import asyncio
from src.domain.entities import GitHubRepo
from src.domain.interfaces import IDeduplicator

class InMemoryDeduplicator(IDeduplicator):
    """
    Thread-safe in-memory deduplication using a set of seen node_ids.
    The asyncio.Lock ensures two coroutines never update _seen simultaneously.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = asyncio.Lock()

    async def filter_fresh_async(self, repos: list[GitHubRepo]) -> list[GitHubRepo]:
        """
        Async version — safe to call from multiple concurrent coroutines.
        The lock ensures only one coroutine updates _seen at a time.
        """
        async with self._lock:
            fresh = [r for r in repos if r.node_id not in self._seen]
            for r in fresh:
                self._seen.add(r.node_id)
            return fresh

    def filter_fresh(self, repos: list[GitHubRepo]) -> list[GitHubRepo]:
        """Sync version — satisfies the IDeduplicator interface."""
        fresh = [r for r in repos if r.node_id not in self._seen]
        for r in fresh:
            self._seen.add(r.node_id)
        return fresh

    def total_seen(self) -> int:
        return len(self._seen)