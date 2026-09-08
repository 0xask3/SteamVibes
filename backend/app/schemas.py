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


class RelaxationStep(BaseModel):
    """One filter widened because the previous set could not fill a page.

    Carries the before and after rather than only a sentence, so a caller can
    render it however it likes and a test can assert on values rather than on
    prose.
    """

    field: str
    was: str
    now: str

    # Pre-rendered because the phrasing differs per field - a dropped tag list
    # and a doubled price are not the same sentence.
    note: str


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

    # Filters widened to fill this page, in the order they were given up. Empty
    # is the normal case. `parsed` above reflects what was ACTUALLY applied -
    # i.e. already relaxed - because that is what produced these results and
    # what the chips must show; this list is the diff that explains it.
    relaxed: list[RelaxationStep] = Field(default_factory=list)

    # None means the ladder never ran - RELAX_FILTERS is off, or run_eval pinned
    # it off to measure recall against a fixed filter set. A number means it ran,
    # INCLUDING when it changed nothing, which is the normal case and the one
    # worth watching: that is the cost relaxation charges every other search.
    relax_ms: float | None = None

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

    # None follows settings.relax_filters. False is how run_eval measures recall
    # against a fixed filter set.
    relax: bool | None = None


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

    `grounded=False` means the model's explanation cited something the game does
    not have and was DISCARDED - `why` is then a deterministic line built from
    the game's own tags. The flag exists so the UI can say which it is showing
    and the eval can count. A fallback presented as an explanation would be the
    exact failure this feature is built to avoid.
    """

    app_id: int
    why: str
    grounded: bool

    # Empty when grounded. One of "unknown_app_id", "unlisted_tag",
    # "prose_tag", "missing" - kept because "4% hallucinated" is three
    # different bugs with three different fixes, and an aggregate hides which.
    discard_reason: str | None = None


class ExplainRequest(BaseModel):
    """POST /api/explain body.

    Deliberately carries app_ids and NOT the games themselves. Name, tags and
    description are looked up server-side, because a verifier that grades the
    model against client-supplied tags proves nothing at all.
    """

    query: str
    app_ids: list[int] = Field(min_length=1, max_length=20)

    # The tags the query asked for, used ONLY to pick which of a game's own tags
    # the deterministic fallback line shows. Never trusted as ground truth - the
    # verifier reads `games.tags` from the database - so a caller sending
    # nonsense here degrades its own fallback text and nothing else.
    wanted_tags: list[str] = Field(default_factory=list)


class ExplainResponse(BaseModel):
    explanations: list[VerifiedExplanation] = Field(default_factory=list)
    elapsed_ms: float


class StageStats(BaseModel):
    """One pipeline stage's latency over the window.

    `max` is carried alongside because at small n it is the honest companion to
    p50 - a single cold search that paid 22s of model load is invisible in a
    median and obvious here.
    """

    n: int
    p50: float

    # None until app/metrics.py has MIN_P95_SAMPLES samples. Below that the
    # "95th percentile" is literally the maximum, and printing the maximum under
    # a p95 label is a wrong label rather than an imprecise number.
    p95: float | None = None

    max: float


class EndpointStats(BaseModel):
    n: int

    # Keyed by the SearchResponse field name (parse_ms, embed_ms, ...). A stage
    # that never ran is ABSENT rather than zero: rerank_ms is None wherever
    # RANK_METHOD is not `rerank`, which is every container, and a 0.0 there
    # would read as a stage that costs nothing.
    stages: dict[str, StageStats] = Field(default_factory=dict)


class StatsResponse(BaseModel):
    """GET /api/stats.

    The three scope fields are part of the payload rather than only the docs,
    because these numbers are the easiest in the project to quote wrongly. The
    window is the last `window` REQUESTS, not a period of time; it covers this
    process only and resets on restart; and it sees API traffic only, so it is
    not comparable with the median run_eval prints.
    """

    window: int
    uptime_s: float

    # Echoed so a caller can render "p95 needs 20 requests, this has 7" rather
    # than an unexplained blank.
    min_p95_samples: int

    search: EndpointStats
    explain: EndpointStats

    # Monotonic since process start, not windowed. A fallback is rare enough
    # that the total is more useful than a rate, and a rate over a sliding
    # window would quietly heal itself.
    fallbacks: dict[str, int] = Field(default_factory=dict)
