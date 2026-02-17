"""
Domain Layer — Interfaces (Abstract Contracts)
-----------------------------------------------
These are ABSTRACT definitions of what the infrastructure must provide.
The domain layer defines the shape; the infrastructure layer implements it.

This is the Dependency Inversion Principle:
  High-level modules (application) should not depend on low-level modules
  (infrastructure). Both should depend on abstractions (these interfaces).

Benefit: you can swap GitHubClient for a FakeGitHubClient in tests
without changing a single line of application code.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from .entities import GitHubRepo

class IRepoFetcher(ABC):
    """
    Contract that any GitHub API client must fulfil.
    The application layer depends on THIS, not on the concrete GitHubClient.
    """

    @abstractmethod
    async def fetch_page(self,query_str: str,cursor:str | None = None) -> tuple[list[GitHubRepo], bool, str | None, int]:
        """
        Fetch one page of search results.

        Returns:
            repos           — list of GitHubRepo domain objects
            has_next_page   — True if more pages exist
            end_cursor      — pagination bookmark for next page
            rate_remaining  — how many API calls remain before limit
        """
        ...


class IRepoStorage(ABC):
    """
    Contract that any storage backend must fulfil.
    Swap PostgreSQL for SQLite or MongoDB without touching application code.
    """

    @abstractmethod
    def upsert_batch(self, repos: list[GitHubRepo]) -> None:
        """Insert new repos or update existing ones. Never duplicates."""
        ...

    @abstractmethod
    def create_run(self) -> int:
        """Create a crawl run audit record. Returns the run ID."""
        ...

    @abstractmethod
    def finish_run(self,run_id:int,total:int,status:str,error:str | None = None) -> None:
        """Mark a crawl run as complete with final stats."""
        ...


class IQueryGenerator(ABC):
    """
    Contract for anything that generates search query strings.
    Makes it easy to swap query strategies without touching the orchestrator.
    """

    @abstractmethod
    def generate(self) -> list[str]:
        """Return a list of GitHub search query strings."""
        ...


class IDeduplicator(ABC):
    """
    Contract for the deduplication service.
    Separated from the orchestrator so each class has one job.
    """

    @abstractmethod
    def filter_fresh(self, repos: list[GitHubRepo]) -> list[GitHubRepo]:
        """Return only repos not seen before. Remembers what it has seen."""
        ...

    @abstractmethod
    def total_seen(self) -> int:
        """Return how many unique repos have been seen so far."""
        ...