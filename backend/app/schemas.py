"""Pydantic models for anything crossing a boundary.

Weekend 2's POST /api/search returns SearchResponse directly, so these are
defined once here rather than redeclared alongside the API.
"""

from decimal import Decimal

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    app_id: int
    name: str
    short_description: str | None = None

    # 1 - cosine distance. Higher is more similar; roughly 0.5-1.0 in practice.
    score: float

    # The normal price, not the scrape-day sale price. See CLAUDE.md.
    list_price_usd: Decimal | None = None
    is_free: bool = False

    total_reviews: int = 0
    positive_ratio: float | None = None

    tags: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult] = Field(default_factory=list)

    requested: int
    # Fewer than requested means the WHERE clause starved the vector index of
    # candidates. Surfaced rather than swallowed - a search that quietly
    # under-delivers is the exact failure this project exists to avoid.
    returned: int

    threshold: int
    embed_ms: float
    query_ms: float

    @property
    def under_delivered(self) -> bool:
        return self.returned < self.requested
