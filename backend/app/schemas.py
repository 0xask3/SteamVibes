"""Pydantic models for anything crossing a boundary.

Weekend 2's POST /api/search returns SearchResponse directly, so these are
defined once here rather than redeclared alongside the API.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, computed_field

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

    # Minimum user reviews. "popular" maps to settings.popular_min_reviews; an
    # explicit count in the query is used as given. Raises search's review
    # floor, never lowers it - see search().
    min_reviews: int | None = None

    # Both set in code by app/title_lookup.py, never by the model - they are
    # stripped from the schema handed to Ollama. A hallucinated app_id would
    # silently remove a real result, and the model cannot know real ones.
    #
    # reference_game is the title the query pointed at ("like elden ring");
    # excluded_app_ids filters it out, but only when the query asked. The name
    # is carried so a filter chip can read "not ELDEN RING" rather than
    # "excluding 1 title".
    reference_game: str | None = None
    excluded_app_ids: list[int] = Field(default_factory=list)

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
                self.min_reviews is not None,
                self.excluded_app_ids,
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
        if self.min_reviews is not None:
            parts.append(f">= {self.min_reviews:,} reviews")
        if self.excluded_app_ids:
            # The name when we have it, so the chip reads "not ELDEN RING".
            label = self.reference_game or f"{len(self.excluded_app_ids)} title(s)"
            parts.append(f"not {label}")
        return parts


class SearchResult(BaseModel):
    app_id: int
    name: str
    short_description: str | None = None

    # 1 - cosine distance. Higher is more similar; roughly 0.5-1.0 in practice.
    score: float

    # What the ORDER BY actually used, once the popularity term is folded in.
    # Equal to `score` when rank_method is "none". Exposed rather than kept
    # internal so a result that outranks a closer match is explicable - the same
    # reason SearchResponse carries `parsed`. Not comparable across rank
    # methods: "log" produces a similarity-scale number and "rrf" a reciprocal
    # -rank one, near 1/k.
    rank_score: float

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

    # None means no cross-encoder ran - RANK_METHOD is not `rerank`. A number
    # means it did, INCLUDING when it failed and search fell back to the SQL
    # ordering, because time spent on a dead container is still time spent.
    # Surfaced because the reranker's whole trade is recall against latency,
    # and a cost nobody can see is a cost nobody can argue about.
    rerank_ms: float | None = None

    # Whether the cross-encoder actually SCORED this pool, as opposed to being
    # asked to and failing. rerank_ms alone cannot tell the two apart, and the
    # difference is not cosmetic: a run that silently fell back returns the
    # baseline ordering while every label on the output still says `rerank`.
    # That produced a full 118-query eval table identical to the baseline, which
    # read as "this model is no better" rather than "this model never ran".
    reranked: bool = False

    # None means no LLM call happened - the caller supplied an already-parsed
    # query, which is what an edited filter chip does. Set by the API endpoint
    # rather than by search(), which knows nothing about parsing.
    parse_ms: float | None = None

    # computed_field, not a bare @property: a plain property is invisible to
    # model_dump_json(), so the browser would never receive this and the
    # "filters starved the index" warning would silently disappear at the API
    # boundary. Still an ordinary property from Python, so search.py's CLI
    # warning is unaffected.
    @computed_field  # type: ignore[prop-decorator]  # pydantic supports this
    @property
    def under_delivered(self) -> bool:
        return self.returned < self.requested


class SearchRequest(BaseModel):
    """POST /api/search body.

    `parsed` is the editable-chip path: when the caller sends one back, its
    filters are used verbatim and no chat model runs. Re-parsing `query`
    instead would re-derive whichever chip the user just removed, and charge
    ~0.7s to do it.
    """

    query: str
    parsed: ParsedQuery | None = None

    limit: int = Field(default=10, ge=1, le=50)
    threshold: int | None = None


class GameDetail(BaseModel):
    """GET /api/game/{app_id}. Everything a click-through page needs.

    Wider than SearchResult on purpose: that one is repeated 10x in a list and
    stays lean, this one is fetched once.
    """

    app_id: int
    name: str
    short_description: str | None = None
    detailed_description: str | None = None

    release_date: date | None = None
    developers: list[str] = Field(default_factory=list)
    publishers: list[str] = Field(default_factory=list)

    # list_price_usd, never price_usd - the snapshot caught a sale. See
    # CLAUDE.md.
    list_price_usd: Decimal | None = None
    discount_pct: int = 0
    is_free: bool = False

    total_reviews: int = 0
    positive_reviews: int = 0
    positive_ratio: float | None = None
    metacritic_score: int | None = None
    estimated_owners: str | None = None

    required_age: int = 0
    platforms: list[str] = Field(default_factory=list)
    header_image: str | None = None

    # Full list here, not the top 5 a SearchResult carries.
    tags: list[str] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
