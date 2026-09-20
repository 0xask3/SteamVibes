"""Pydantic models for anything crossing a boundary.

The API returns these directly, so they are defined once here rather than
redeclared alongside it.
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

    # FIELD ORDER IS LOAD-BEARING: this schema is handed to Ollama's `format`,
    # so the model emits fields in declaration order and cannot revise an
    # earlier one. semantic_query stays LAST because it depends on all the
    # others; declared first, both models returned the sentence unstripped.
    max_price_usd: float | None = None
    min_price_usd: float | None = None

    required_tags: list[str] = Field(default_factory=list)
    excluded_tags: list[str] = Field(default_factory=list)

    # ANDed: ["mac", "linux"] means runs on both, not either.
    platforms: list[Platform] = Field(default_factory=list)

    released_after: int | None = None  # year
    multiplayer: bool | None = None
    max_required_age: int | None = None

    # Minimum user reviews. Raises search's review floor, never lowers it.
    min_reviews: int | None = None

    # Set in code by app/title_lookup.py and stripped from the schema handed to
    # Ollama: a hallucinated app_id would silently remove a real result. The
    # name is carried so a chip can read "not ELDEN RING".
    reference_game: str | None = None
    excluded_app_ids: list[int] = Field(default_factory=list)

    # The part that gets embedded. Everything else is a WHERE clause.
    semantic_query: str

    def describe(self) -> list[str]:
        """Human-readable filter list, for the CLI and the UI chips."""
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

    # What the ORDER BY used, once the popularity term is folded in. Equal to
    # `score` when rank_method is "none", and NOT comparable across rank
    # methods: "log" is on a similarity scale, "rrf" near 1/k.
    rank_score: float

    # The normal price, not the scrape-day sale price. See CLAUDE.md.
    list_price_usd: Decimal | None = None
    is_free: bool = False

    total_reviews: int = 0
    positive_ratio: float | None = None

    tags: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)


class RelaxationStep(BaseModel):
    """One filter widened because the previous set could not fill a page.

    Carries before and after, not only a sentence, so a test can assert on
    values rather than on prose.
    """

    field: str
    was: str
    now: str

    # Pre-rendered: the phrasing differs per field.
    note: str


class SearchResponse(BaseModel):
    # What was ACTUALLY applied, already relaxed - the editable chips render it.
    parsed: ParsedQuery
    results: list[SearchResult] = Field(default_factory=list)

    # Tags that matched nothing real. Reported rather than silently yielding
    # zero rows: `Base Building` vs `Base-Building` returns nothing, no error.
    unknown_tags: list[str] = Field(default_factory=list)

    requested: int
    # Fewer than requested means the WHERE clause starved the index. Surfaced
    # rather than swallowed.
    returned: int

    threshold: int

    # The diff explaining `parsed`, in the order filters were given up. Empty is
    # the normal case.
    relaxed: list[RelaxationStep] = Field(default_factory=list)

    # None means the ladder never ran. A number means it did, INCLUDING when it
    # changed nothing - that is the cost relaxation charges every search.
    relax_ms: float | None = None

    embed_ms: float
    query_ms: float

    # None means no cross-encoder ran. A number means it did, INCLUDING when it
    # failed and search fell back - time spent is still time spent.
    rerank_ms: float | None = None

    # Whether the cross-encoder actually SCORED this pool, as opposed to being
    # asked to and failing. rerank_ms alone cannot tell those apart, and a
    # silent fallback returns the baseline ordering under this model's label -
    # which once read as "no better" rather than "never ran". failures.md #36.
    reranked: bool = False

    # None means no LLM call happened: the caller supplied an already-parsed
    # query, which is what an edited chip does. Set by the endpoint, not by
    # search(), which knows nothing about parsing.
    parse_ms: float | None = None

    # computed_field, not a bare @property: a plain property is invisible to
    # model_dump_json(), so this warning would vanish at the API boundary.
    @computed_field  # type: ignore[prop-decorator]  # pydantic supports this
    @property
    def under_delivered(self) -> bool:
        return self.returned < self.requested


class SearchRequest(BaseModel):
    """POST /api/search body.

    `parsed` is the editable-chip path: sent back, its filters are used verbatim
    and no chat model runs. Re-parsing would re-derive the chip just removed.
    """

    query: str
    parsed: ParsedQuery | None = None

    limit: int = Field(default=10, ge=1, le=50)
    threshold: int | None = None

    # None follows settings.relax_filters; False is how run_eval pins it off.
    relax: bool | None = None


class GameDetail(BaseModel):
    """GET /api/game/{app_id}. Wider than SearchResult, which is repeated 10x
    in a list and stays lean; this one is fetched once.
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


class Explanation(BaseModel):
    """One "why this matches" line, and whether it survived verification.

    FIELD ORDER IS LOAD-BEARING, the same way `semantic_query` must be last in
    ParsedQuery. Ollama emits fields in declaration order, so `cited_tags`
    before `why` makes the model commit to a tag list and then write prose
    consistent with it, rather than justifying finished prose after the fact.

    Every field is required by construction - none carries a default. That is
    deliberate rather than incidental: an OPTIONAL property in a
    constrained-decoding schema is a grammar branch the model may skip, and a
    schema generated from Pydantic is optional by accident. Anything added here
    needs the same check. See failures.md #33.
    """

    app_id: int
    cited_tags: list[str]
    why: str


class VerifiedExplanation(BaseModel):
    """What the API returns: the line, plus whether a model actually wrote it.

    `grounded=False` means the model cited something the game does not have and
    was DISCARDED; `why` is then built deterministically from the game's own
    tags. The flag travels so the UI can say which it is showing - a fallback
    presented as an explanation is the failure this feature exists to prevent.
    """

    app_id: int
    why: str
    grounded: bool

    # Empty when grounded. One of "unknown_app_id", "unlisted_tag",
    # "prose_tag", "missing": one hallucination rate is several different bugs.
    discard_reason: str | None = None


class ExplainRequest(BaseModel):
    """POST /api/explain body.

    Carries app_ids and NOT the games themselves: a verifier fed
    client-supplied tags is checking the model against the client.
    """

    # MUST be `parsed.semantic_query`, never the typed text. The model sees tags
    # but no platforms or prices, so a constraint left in reads as a mismatch it
    # is told to report - the raw query denied Linux for 15 of 15 Linux games,
    # the stripped one for 0 of 15.
    query: str
    app_ids: list[int] = Field(min_length=1, max_length=20)

    # Used ONLY to pick which of a game's own tags the fallback line shows.
    # Never ground truth - the verifier reads `games.tags` from the database.
    wanted_tags: list[str] = Field(default_factory=list)


class ExplainResponse(BaseModel):
    explanations: list[VerifiedExplanation] = Field(default_factory=list)
    elapsed_ms: float


class StageStats(BaseModel):
    """One pipeline stage's latency over the window.

    `max` rides alongside p50: a cold search that paid 22s of model load is
    invisible in a median and obvious here.
    """

    n: int
    p50: float

    # None below app/metrics.py's MIN_P95_SAMPLES, where the "95th percentile"
    # is literally the maximum - a wrong label, not an imprecise number.
    p95: float | None = None

    max: float


class EndpointStats(BaseModel):
    n: int

    # Keyed by the SearchResponse field name. A stage that never ran is ABSENT
    # rather than zero, which would read as a stage that costs nothing.
    stages: dict[str, StageStats] = Field(default_factory=dict)


class StatsResponse(BaseModel):
    """GET /api/stats.

    Every number ships with its scope, because these are the easiest in the
    project to quote wrongly: the window is the last `window` REQUESTS, not a
    period of time; it is per-process and resets on restart; and it records at
    the endpoint, so it is not the same measurement run_eval prints.
    """

    window: int
    uptime_s: float

    # Echoed so a caller can explain a blank p95 rather than just showing one.
    min_p95_samples: int

    search: EndpointStats
    explain: EndpointStats

    # Monotonic since process start, not windowed: a rate over a sliding window
    # would quietly heal itself.
    fallbacks: dict[str, int] = Field(default_factory=dict)
