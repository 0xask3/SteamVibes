"""Pydantic models for anything crossing a boundary.

Weekend 2's POST /api/search returns SearchResponse directly, so these are
defined once here rather than redeclared alongside the API.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal["windows", "mac", "linux"]


class ParsedQuery(BaseModel):
    """A search request split into a vibe and hard constraints.

    Filled by hand from CLI flags or by app/query_parser.py from natural
    language. Same object either way, so the parser is another way to produce
    one rather than a second implementation of filtering.

    Prices are USD, not EUR as BUILD_PLAN.md assumed - the Kaggle source is
    Steam's US storefront. max_required_age came out of eval/failures.md #8.
    """

    # FIELD ORDER IS LOAD-BEARING. This schema is handed to Ollama's `format`,
    # which constrains generation, so the model emits fields in declaration
    # order and cannot revise an earlier one. semantic_query is therefore last:
    # it is the only field whose value depends on all the others, because it is
    # the query with every extracted constraint removed. When it came first,
    # both models returned the original sentence unchanged - they had not yet
    # worked out what to strip. See eval/failures.md.
    max_price_usd: float | None = None
    min_price_usd: float | None = None

    required_tags: list[str] = Field(default_factory=list)
    excluded_tags: list[str] = Field(default_factory=list)

    # ANDed: ["mac", "linux"] means runs on both, not either.
    platforms: list[Platform] = Field(default_factory=list)

    released_after: int | None = None  # year
    multiplayer: bool | None = None
    max_required_age: int | None = None

    # The part that gets embedded. Everything else is a WHERE clause.
    semantic_query: str

    def has_filters(self) -> bool:
        """True when anything beyond the semantic query is set."""
        return any(
            (
                self.max_price_usd is not None,
                self.min_price_usd is not None,
                self.required_tags,
                self.excluded_tags,
                self.platforms,
                self.released_after is not None,
                self.multiplayer is not None,
                self.max_required_age is not None,
            )
        )

    def describe(self) -> list[str]:
        """Human-readable filter list, for the CLI and later the UI chips."""
        parts: list[str] = []
        if self.min_price_usd is not None:
            parts.append(f">= ${self.min_price_usd:g}")
        if self.max_price_usd is not None:
            parts.append(f"<= ${self.max_price_usd:g}")
        parts.extend(self.platforms)
        parts.extend(self.required_tags)
        parts.extend(f"not {tag}" for tag in self.excluded_tags)
        if self.released_after is not None:
            parts.append(f"after {self.released_after}")
        if self.multiplayer is True:
            parts.append("multiplayer")
        elif self.multiplayer is False:
            parts.append("singleplayer")
        if self.max_required_age is not None:
            parts.append(f"age <= {self.max_required_age}")
        return parts


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
    # Returned alongside the results so the caller can see what was actually
    # applied. BUILD_PLAN.md's editable filter chips render this.
    parsed: ParsedQuery
    results: list[SearchResult] = Field(default_factory=list)

    # Tags that matched nothing in the real vocabulary. Reported rather than
    # silently yielding zero rows - `Base Building` vs `Base-Building` returns
    # nothing with no error, which is the failure the parser's fuzzy matching
    # will exist to prevent.
    unknown_tags: list[str] = Field(default_factory=list)

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
