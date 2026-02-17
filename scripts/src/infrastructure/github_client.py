from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from src.domain.entities import GitHubRepo
from src.domain.interfaces import IRepoFetcher

log = logging.getLogger(__name__)

GITHUB_API_URL= "https://api.github.com/graphql"
PAGE_SIZE= 100
RATE_LIMIT_SLEEP= 60
MAX_RETRIES= 5

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
    """Raised when GitHub explicitly returns a RATE_LIMITED error."""
    pass


class GitHubClient(IRepoFetcher):
    """
    Concrete implementation of IRepoFetcher for GitHub's GraphQL API.

    The constructor receives an httpx.AsyncClient (injected) rather than
    creating one internally. This lets callers control the client lifecycle
    and makes testing trivial — just pass in a mock client.
    """

    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._client = client
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }

    # Anti-Corruption Layer
    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        """Convert GitHub's ISO datetime string to Python datetime."""
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _parse_node(self, node: dict) -> GitHubRepo | None:
        """
        ANTI-CORRUPTION LAYER — translates GitHub's raw API response
        into our clean, stable GitHubRepo domain object.

        GitHub sends:          We store as:
          "nameWithOwner"   →  name_with_owner
          "stargazerCount"  →  star_count etc.

        If GitHub renames a field, fix it HERE only - nowhere else.
        """
        try:
            return GitHubRepo(
                node_id          = node["id"],
                name_with_owner  = node["nameWithOwner"],
                name             = node["name"],
                owner_login      = node["owner"]["login"],
                description      = node.get("description"),
                primary_language = (
                    node["primaryLanguage"]["name"]
                    if node.get("primaryLanguage") else None
                ),
                is_private  = node.get("isPrivate", False),
                star_count  = node.get("stargazerCount", 0),
                created_at  = self._parse_datetime(node.get("createdAt")),
                updated_at  = self._parse_datetime(node.get("updatedAt")),
            )
        except (KeyError, TypeError) as exc:
            log.debug("Skipping malformed API node %s: %s", node.get("id"), exc)
            return None

    # IRepoFetcher implementation
    async def fetch_page(self,query_str: str,cursor:str | None = None) -> tuple[list[GitHubRepo], bool, str | None, int]:
        """
        Fetch one page of GitHub search results with retry logic.

        Returns:
            repos — clean GitHubRepo domain objects
            has_next_page  — whether more pages exist
            end_cursor — bookmark to pass as cursor on next call
            rate_remaining — remaining API quota
        """
        variables = {
            "query": query_str,
            "first": PAGE_SIZE,
            "after": cursor,
        }

        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.post(
                    GITHUB_API_URL,
                    headers=self._headers,
                    json={"query": GRAPHQL_QUERY, "variables": variables},
                    timeout=30.0,
                )
                response.raise_for_status()
                data = response.json()

                # Check for GraphQL-level errors (different from HTTP errors)
                if "errors" in data:
                    for err in data["errors"]:
                        if err.get("type") == "RATE_LIMITED":
                            raise RateLimitError()
                    log.warning("GraphQL errors for query %.60s: %s", query_str, data["errors"])

                rate = data["data"]["rateLimit"]
                search = data["data"]["search"]
                page_info = search["pageInfo"]

                # Apply anti-corruption layer to every node
                repos = [parsed for node in search["nodes"] if (parsed := self._parse_node(node)) is not None]

                return (
                    repos,
                    page_info["hasNextPage"],
                    page_info["endCursor"],
                    rate["remaining"],
                )

            except RateLimitError:
                log.info("Rate limited - sleeping %ds before retry …", RATE_LIMIT_SLEEP)
                await asyncio.sleep(RATE_LIMIT_SLEEP)

            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                wait = 2 ** attempt   # exponential backoff: 2s, 4s, 8s, 16s, 32s
                log.warning( "HTTP error attempt %d/%d: %s — retrying in %ds", attempt + 1, MAX_RETRIES, exc, wait)
                await asyncio.sleep(wait)

        raise RuntimeError(
            f"Exhausted {MAX_RETRIES} retries for query: {query_str[:80]}"
        )