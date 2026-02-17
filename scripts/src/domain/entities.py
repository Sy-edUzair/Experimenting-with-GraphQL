from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GitHubRepo:
    """
    Immutable domain entity representing a GitHub repository.

    frozen=True guarantees immutability — once created, no field
    can ever be changed. Data flows one way: API → entity → database.

    Field names are OURS (snake_case), not GitHub's (camelCase).
    The translation happens in the anti-corruption layer, not here.
    """
    node_id:          str
    name_with_owner:  str
    name:             str
    owner_login:      str
    description:      str | None
    primary_language: str | None
    is_private:       bool
    star_count:       int
    created_at:       datetime | None
    updated_at:       datetime | None


@dataclass(frozen=True)
class CrawlResult:
    """
    Immutable value object summarising a completed crawl run.
    Returned by the application service when crawling finishes.
    """
    run_id:        int
    total_repos:   int
    status:        str          
    elapsed_secs:  float
    error_message: str | None = None