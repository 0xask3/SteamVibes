"""Typed settings, read once from the repo-root .env.

Anything missing raises at import time rather than halfway through a 35-minute
embedding run.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app -> backend -> repo root
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

    # Changing this changes what every stored vector means, so it needs
    # `embed_all --reload` rather than a restart, and the query/document
    # prefixes follow it in app/embedding.py. The vector WIDTH is not a setting;
    # it is EMBEDDING_DIM in app/models.py, fixed by the migration. arctic is
    # the measured winner over qwen3-embedding:0.6b and bge-m3 - failures.md #30.
    embed_model: str = "snowflake-arctic-embed2"

    # Ollama's physical batch. It checks the PACKED token count of several
    # inputs at once, so an ordinary batch can be rejected while every text in
    # it is tiny; embed_texts halves and retries. NOTES.md 2026-09-06.
    embed_num_batch: int = 4096

    # Ollama's 5m default evicts the model, and an idle CLI then pays ~18s
    # reloading it to do ~20ms of work.
    ollama_keep_alive: str = "30m"

    # A setting rather than a constant so models can be compared without
    # touching code - see eval/compare_parsers.py.
    chat_model: str = "qwen3.5:9b"

    # The parser extracts fields, it does not reason, and it sits in the request
    # path. None omits the flag entirely, which a model predating it requires -
    # Ollama 400s on `think` rather than ignoring it.
    chat_think: bool | None = False

    # Must comfortably hold the ~1,800-token vocabulary prompt: an overflow
    # truncates silently from the end, dropping tags off the list, which is the
    # exact failure passing all 452 was meant to remove.
    chat_num_ctx: int = 8192

    # Games with total_reviews <= this are hidden from results. Everything is
    # embedded regardless, so this is a runtime knob.
    review_threshold: int = 10

    # What a bare "popular" means, in reviews - roughly the top 12% of the
    # searchable corpus. Formatted into the prompt rather than written there, so
    # this stays the single source of truth.
    popular_min_reviews: int = 1_000

    # Minimum reviews for a game name to be recognised inside a query. Common
    # words are real titles (`Nothing` has 9,260 reviews), so a lower floor makes
    # "nothing scary" match a horror game. See failures.md #21.
    title_match_min_reviews: int = 50_000

    # How hard HNSW searches for the candidate pool, and the reason is
    # reproducibility before recall: 800 is the smallest value where results stop
    # depending on which graph the parallel, non-deterministic index build
    # produced. Do not lower it. See failures.md #42.
    hnsw_ef_search: int = 800

    # How many index hits get reranked. Must not exceed hnsw_ef_search: stage 1
    # cannot return more candidates than it was allowed to look at, and coming up
    # short is silent.
    rerank_candidates: int = 200

    # How prominence enters the ranking, which cosine has none of:
    #   none   - pure cosine
    #   log    - (1 - dist) + w * log10(1 + reviews) / LOG10_MAX_REVIEWS
    #   rrf    - reciprocal rank fusion of the cosine and popularity orderings
    #   rerank - a cross-encoder's rank replaces the cosine rank inside that same
    #            rrf sum; POPULARITY_WEIGHT=0 collapses it to pure cross-encoder
    #
    # rrf over log at matched tail cost, because log's weight is calibrated
    # against one model's cosine spread while rrf reads only ranks. w=0.20 is the
    # smallest weight that fixes the observed defect - Cities: Skylines II above
    # a 27-review asset flip for "city builder" - and leaves ~70% of results in
    # the tail; higher weights buy recall by deleting the long tail. The weight
    # is chosen by `tail` and the tail-cost counter-metric, never by `core` or
    # `specific`. See failures.md #26 and #36. docker-compose overrides this back
    # to `rrf`, because the container has neither torch nor the GPU.
    rank_method: Literal["none", "log", "rrf", "rerank"] = "rerank"
    popularity_weight: float = 0.20

    # RRF's rank-smoothing constant, the value from the original paper.
    rrf_k: int = 60

    # A Hugging Face model id loaded IN-PROCESS by app/rerank.py, not a served
    # endpoint - see its module docstring for why neither serving route works.
    # Qwen3 and bge-reranker-v2-m3 are NOT distinguishable on recall; Qwen3 is
    # the default on a deterministic behaviour the eval cannot score, and costs
    # 4x the latency for it. See failures.md #36 and #37.
    rerank_model: str = "tomaarsen/Qwen3-Reranker-0.6B-seq-cls"

    # CPU is not a slower version of this measurement, it is an unusable one, and
    # sentence-transformers falls back to it without saying so.
    rerank_device: str = "cuda"

    # Pairs per forward pass, and NOT only a speed knob: a different batch shape
    # changes the fp16 arithmetic and can reorder near-ties. 32 is measured -
    # less VRAM and faster than 128, with every top-10 set unchanged. Any other
    # value needs compare_runs at 0 wins and 0 losses first.
    rerank_batch_size: int = 32

    # The task description handed to an instruction-following reranker, ignored
    # by models that cannot take one. A tuned prompt, so re-run the eval after
    # touching it. English on the model card's advice, even for German queries.
    rerank_instruction: str = (
        "Given a description of the feeling or content a player wants, retrieve "
        "video games matching it."
    )

    # Executes Python from the model repo at load time, so it is opt-in per
    # model: a setting somebody has to type is a decision somebody made.
    rerank_trust_remote_code: bool = False

    # Optional. Unauthenticated Hub downloads are rate limited hard enough to
    # stall a model download, which looks like a hang. SecretStr so it cannot
    # reach a log line or a traceback; app/rerank.py copies it into HF_TOKEN,
    # which is what huggingface_hub actually reads.
    hugging_face: SecretStr | None = None

    # Whether the relaxation ladder runs at all; the ladder and the never-relax
    # list live in app/relax.py. OFF in run_eval, which must measure recall
    # against a FIXED filter set.
    relax_filters: bool = True

    # Rows that must pass the filters before relaxation stops. None means the
    # requested limit, deliberately conservative: relaxing while the filters can
    # still fill the page overrides a constraint the user actually stated.
    relax_target_rows: int | None = None

    # How many REQUESTS /api/stats keeps per endpoint, in memory. A count of
    # requests, not a period of time, which is why the endpoint returns it.
    stats_window: int = 500

    @model_validator(mode="after")
    def _pool_fits_in_search(self) -> Settings:
        """Refuse a rerank pool the index cannot fill.

        Stage 1 coming up short raises nothing - the reranker just gets a
        smaller pool and the results quietly get worse.
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
