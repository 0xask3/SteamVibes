"""Typed settings, read once from the repo-root .env.

Anything missing raises at import time rather than halfway through a 35-minute
embedding run.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/app -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env also holds POSTGRES_* for docker compose
    )

    # No default: a missing DATABASE_URL is a bug, not something to guess at.
    database_url: str

    ollama_base_url: str = "http://127.0.0.1:11434"

    # The embedding model. Changing this changes what every stored vector means,
    # so it needs `embed_all --reload`, not just a restart - and the query/
    # document prefixes move with it (app/embedding.py). The vector width is NOT
    # a setting: it is EMBEDDING_DIM in app/models.py, fixed by the migration.
    embed_model: str = "nomic-embed-text"

    # How long Ollama keeps the model in VRAM after a request. Its default is
    # 5m, after which the next query pays an ~18s cold load - which dwarfs the
    # ~20ms the embedding itself takes. The model is 323MB, so holding it is
    # cheap. "-1" never unloads; "0" unloads immediately.
    ollama_keep_alive: str = "30m"

    # Chat model for the query parser. A setting rather than a constant so
    # models can be compared without touching code - see
    # eval/compare_parsers.py.
    chat_model: str = "qwen3.5:9b"

    # Qwen3.5 is a hybrid reasoning model: it emits a thinking block before the
    # answer unless told not to. The parser sits in the request path and the
    # task is extraction, not reasoning, so thinking is latency we do not buy
    # anything with. None omits the flag entirely, which a model with no
    # thinking mode requires - Ollama 400s on `think` rather than ignoring it.
    chat_think: bool | None = False

    # The parser's system prompt carries all 452 tags - ~1,400 tokens of
    # vocabulary plus the rules. Ollama's default context is small enough that
    # this is uncomfortably close to it, and an overflow truncates silently,
    # dropping tags off the end of the list. That is the exact failure passing
    # the full vocabulary was meant to remove, so the window is set explicitly.
    chat_num_ctx: int = 8192

    # Games with total_reviews <= this are hidden from search results. A knob,
    # not a constant — Weekend 3 measures recall at several values.
    review_threshold: int = 10

    # What a bare "popular" means, in reviews. p90 of the searchable corpus is
    # 1,628 and p50 is 64, so this is roughly the top 12% - deliberately
    # generous, because a long-tail discovery engine should not treat a
    # well-loved 1,200-review indie as obscure. Never inlined in the prompt;
    # it is formatted in, so this stays the single source of truth.
    popular_min_reviews: int = 1_000

    # Minimum reviews for a game name to be recognised inside a query. Common
    # English words are real Steam titles - `Nothing` (9,260 reviews),
    # `Something`, `SELF`, `Dollar`, `Beat` - so "nothing scary" matches a game
    # without a floor here. At 50,000 the seven test queries produced zero
    # false positives while still finding ELDEN RING and Stardew Valley.
    # See failures.md #21.
    title_match_min_reviews: int = 50_000

    # pgvector's ef_search default of 40 is too small here: measured recall@10
    # at threshold 10 was 15.0% against the 18.3% of the exact scan the index
    # replaced, and 200 recovers that exactly for 45ms -> 53ms. It is also the
    # pool the reranker draws from, so it is a knob, not a constant.
    # See NOTES.md 2026-09-04.
    hnsw_ef_search: int = 200

    # How many index hits get reranked. Must not exceed hnsw_ef_search: stage 1
    # cannot return more candidates than it was allowed to look at, and coming
    # up short is silent - no error, just a smaller pool.
    rerank_candidates: int = 200

    # How prominence enters the ranking. Cosine similarity has none of its own:
    # `Square City Builder` (27 reviews) ties `Cities: Skylines II` (73,524) for
    # "city builder", and eight of that top ten are under 1,000 reviews.
    #
    #   none - pure cosine, exactly today's ordering
    #   log  - (1 - dist) + w * log10(1 + reviews) / LOG10_MAX_REVIEWS
    #   rrf  - reciprocal rank fusion of the cosine and popularity orderings
    #
    # Measured at matched tail cost the two are equivalent - log 0.05 and rrf
    # 0.20 both give 25.0% recall@10 at ~70% of results under 1,000 reviews, and
    # log 0.20 and rrf 1.00 both give 31.1%. So the tiebreak is durability, and
    # rrf wins it: log's weight is calibrated against the model's cosine spread
    # (qwen3's top 10 spans 0.752-0.696, arctic's 0.577-0.502), while rrf reads
    # only ranks and means the same thing after a model swap. That matters here
    # because the model choice is itself provisional - see CLAUDE.md.
    #
    # 0.20 is NOT the weight that maximises recall. recall@10 peaks at 35.6%
    # (rrf 2.00), but every one of the 37 ground-truth games has >=11,267
    # reviews, so recall rises with the weight until the corpus is gone: at the
    # peak only 4% of returned results have under 1,000 reviews, against 79%
    # unweighted. That is REVIEW_THRESHOLD=10000 by another route, and we
    # already refused that trade. 0.20 is the smallest weight that fixes the
    # observed defect - Cities: Skylines II above a 27-review asset flip for
    # "city builder" - while leaving 70% of results in the tail.
    # See failures.md #26.
    rank_method: Literal["none", "log", "rrf"] = "rrf"
    popularity_weight: float = 0.20

    # RRF's rank-smoothing constant. 60 is the value from the original paper and
    # the usual default; it decides how quickly the benefit of being ranked
    # higher flattens out.
    rrf_k: int = 60

    @model_validator(mode="after")
    def _pool_fits_in_search(self) -> Settings:
        """Refuse a rerank pool the index cannot fill.

        Stage 1 cannot return more candidates than ef_search let it look at, and
        coming up short raises nothing - the reranker just gets a smaller pool
        and the results quietly get worse. Same class of failure as the
        iterative_scan shortfall in search.py, so it is checked rather than
        commented.
        """
        if self.rerank_candidates > self.hnsw_ef_search:
            raise ValueError(
                f"rerank_candidates ({self.rerank_candidates}) exceeds "
                f"hnsw_ef_search ({self.hnsw_ef_search}), so stage 1 cannot "
                "supply the pool. Raise HNSW_EF_SEARCH to at least match."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so the .env is parsed once per process."""
    return Settings()  # type: ignore[call-arg]  # values come from the env file


settings = get_settings()
