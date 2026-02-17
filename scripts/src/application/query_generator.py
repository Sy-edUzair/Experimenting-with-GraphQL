from __future__ import annotations
import logging
from src.domain.interfaces import IQueryGenerator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Search dimensions
# ---------------------------------------------------------------------------
# Combining 3 dimensions gives thousands of unique non-overlapping queries.
# Each GitHub search query returns at most 1,000 results.
#
#   20 languages × 8 star ranges × 10 years = 1,600 queries
#   + 20 languages × 8 star ranges (no year)  = 160 fallback queries
#   Total: ~1,760 queries × 1,000 max each = 1,760,000 potential repos

LANGUAGES = [
    "Python", "JavaScript", "TypeScript", "Java", "Go",
    "Rust", "C++", "C", "C#", "Ruby",
    "PHP", "Swift", "Kotlin", "Scala", "Shell",
    "HTML", "CSS", "Vue", "Dart", "R",
]

STAR_RANGES = [
    "stars:>10000",
    "stars:1000..9999",
    "stars:500..999",
    "stars:100..499",
    "stars:50..99",
    "stars:20..49",
    "stars:10..19",
    "stars:1..9",
]

YEAR_RANGES = [
    "created:2024-01-01..2024-12-31",
    "created:2023-01-01..2023-12-31",
    "created:2022-01-01..2022-12-31",
    "created:2021-01-01..2021-12-31",
    "created:2020-01-01..2020-12-31",
    "created:2019-01-01..2019-12-31",
    "created:2018-01-01..2018-12-31",
    "created:2017-01-01..2017-12-31",
    "created:2016-01-01..2016-12-31",
    "created:<2016-01-01",
]


class MultiDimensionalQueryGenerator(IQueryGenerator):
    """
    Generates search queries by combining language × stars × year.

    Each combination is a unique, non-overlapping GitHub search query.
    Year filtering splits the search space to avoid the 1,000-result
    cap on any single query — more combinations = more repos reachable.
    """

    def generate(self) -> list[str]:
        queries: list[str] = []

        # Primary: all three dimensions combined
        for lang in LANGUAGES:
            for stars in STAR_RANGES:
                for year in YEAR_RANGES:
                    queries.append(f"language:{lang} {stars} {year}")

        # Fallback: language + stars without year, catches repos that have no creation date metadata
        for lang in LANGUAGES:
            for stars in STAR_RANGES:
                queries.append(f"language:{lang} {stars}")

        log.info(
            "QueryGenerator produced %d unique queries "
            "(%d languages × %d star ranges × %d years + %d fallbacks)",
            len(queries),
            len(LANGUAGES),
            len(STAR_RANGES),
            len(YEAR_RANGES),
            len(LANGUAGES) * len(STAR_RANGES),
        )
        return queries