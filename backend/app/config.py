"""Typed settings, read once from the repo-root .env.

Anything missing raises at import time rather than halfway through a 35-minute
embedding run.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
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
    #
    # The default is not decoration - it is what runs when .env is absent, and
    # it was left at nomic-embed-text for two migrations after the project
    # stopped using it. nomic emits 768 dimensions against a vector(1024)
    # column, so that default could only ever have failed. It failed loudly
    # (embed_texts raises on the dimension), which is the one reason it survived
    # unnoticed. arctic is the measured winner over qwen3-embedding:0.6b and
    # bge-m3 on 118 labelled queries - failures.md #30.
    embed_model: str = "snowflake-arctic-embed2"

    # Ollama's physical batch (n_ubatch), sent as an option on every embed call.
    # It packs several inputs into one server task, and the PACKED token count is
    # what gets checked against this - so a batch of ordinary rows can be
    # rejected while every text in it is tiny. A 130,651-row run died on a task
    # of 3,002 tokens with 2,048 as the default, between two rows of 114 and 134
    # (NOTES.md 2026-09-06). Capped by the model's context, so raising this past
    # n_ctx does nothing. embed_texts still halves a rejected batch, because this
    # raises the ceiling rather than removing it.
    embed_num_batch: int = 4096

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
    #   rerank - a cross-encoder scores each pooled candidate against the query,
    #            and its RANK replaces the cosine rank inside the same rrf sum.
    #            Same k, same weight, so the number stays comparable to `rrf`
    #            and the tail-cost counter-metric keeps meaning what it meant.
    #            POPULARITY_WEIGHT=0 makes it pure cross-encoder.
    #
    # Why a cross-encoder at all, measured rather than assumed: of the 76
    # labelled targets NOT already in the top 10, 40 are inside the 200-row pool
    # and 36 are not. On the two tiers that can price a ranking change it is
    # lopsided - ALL 9 tail misses and 9 of 11 specific misses are already in
    # the pool, so retrieval found them and the ordering buried them. A
    # bi-encoder embeds query and document separately and never reads them
    # together; a cross-encoder does. Ceiling: 112 of 148 targets are in the
    # pool at all, so 75.7% overall is the most any reranker can produce here.
    # Default is `rerank` on measurement: 68.8% overall against rrf's 60.5%,
    # core 27.2 against 17.8, specific 88.6 against 79.5, tail 77.3 against
    # 70.5, with the tail-cost counter-metric slightly BETTER (71% under 1k
    # against 74%) - so it is not buying recall by deleting the long tail.
    # See failures.md #36. docker-compose overrides this back to `rrf` because
    # the container has neither torch nor the GPU.
    rank_method: Literal["none", "log", "rrf", "rerank"] = "rerank"
    popularity_weight: float = 0.20

    # RRF's rank-smoothing constant. 60 is the value from the original paper and
    # the usual default; it decides how quickly the benefit of being ranked
    # higher flattens out.
    rrf_k: int = 60

    # The cross-encoder, loaded IN-PROCESS by app/rerank.py. A Hugging Face
    # model id, not a served endpoint, because neither serving route works here:
    # Ollama has no rerank endpoint at all, and TEI cannot reach the GPU through
    # Docker Desktop's WSL2 backend. See app/rerank.py's module docstring.
    #
    # Changing this changes what the ranking MEANS, so a bake-off arm is one
    # .env line plus a restart - and verify_rerank_model() refuses to let an
    # eval run against a model that did not actually load.
    # Qwen3 and bge-reranker-v2-m3 are NOT distinguishable on recall: +2.7%
    # overall with a 95% CI of [-2.1%, +7.6%], 11 wins to 5, and 102 of 118
    # queries identical. Do not quote the point estimate as a win.
    #
    # It is the default on a DETERMINISTIC behaviour instead, which the eval
    # cannot score and a bootstrap therefore cannot doubt: for "city builder"
    # bge drops Cities: Skylines II to rank 37, because a 78-review game NAMED
    # `City Builder` is more literally related, while Qwen3 holds it at rank 1.
    # That is the instruction-following difference - bge scores -0.01 on
    # FollowIR, at chance, so it can only answer "how related are these two
    # texts", which is not the question a vibe search asks.
    #
    # Costs 4x the latency, 1,021ms against 264ms, for recall that cannot be
    # told apart. On any latency budget, bge is the right call.
    # See failures.md #36 and #37.
    #
    # The chat template in app/rerank.py is NOT optional decoration - handed a
    # bare pair this model scores 8.1% overall, because the yes/no logit it was
    # trained to emit lands after that exact assistant preamble.
    rerank_model: str = "tomaarsen/Qwen3-Reranker-0.6B-seq-cls"

    # "cuda" or "cpu". CPU is not a slower version of this measurement, it is an
    # unusable one: TEI on CPU never finished warming up a 568M cross-encoder,
    # and 118 queries x 200 candidates would run for hours. cuda is the default
    # so that a silent CPU fallback becomes an error rather than a mystery -
    # sentence-transformers will happily use CPU without saying so.
    rerank_device: str = "cuda"

    # Pairs per forward pass. Not a network batch - it is what
    # sentence-transformers hands the GPU at once, trading VRAM for throughput.
    # 128 pairs at 512 tokens sits comfortably beside a 6.6GB chat model on
    # 16GB. Lower this before lowering the pool if VRAM gets tight: the pool
    # size decides which targets are REACHABLE, this only decides how fast.
    rerank_batch_size: int = 128

    # The task description handed to an INSTRUCTION-FOLLOWING reranker. Ignored
    # by models that do not take one - bge-reranker-v2-m3 scores -0.01 on
    # FollowIR, i.e. at chance, and has no way to receive this at all.
    #
    # It is a tuned prompt, so it is a setting rather than a constant: three
    # separate prompt edits in this project have silently destroyed a working
    # filter (failures.md #13, #22, #32), and the only defence that has ever
    # worked is being able to change one and re-run the eval. English on the
    # model card's advice, even for German queries.
    rerank_instruction: str = (
        "Given a description of the feeling or content a player wants, retrieve "
        "video games matching it."
    )

    # Some rerankers ship a CUSTOM architecture and will not load without this -
    # gte-multilingual-reranker-base pulls `Alibaba-NLP/new-impl`. It means
    # executing Python from the model repo at load time, so it is opt-in per
    # model rather than on by default: a setting somebody has to type is a
    # decision somebody made.
    rerank_trust_remote_code: bool = False

    # Optional Hugging Face token. Unauthenticated Hub downloads are rate
    # limited hard enough to stall a 1.2GB model at 9KB, which looks like a hang
    # rather than a limit. SecretStr so it cannot reach a log line or a
    # traceback - pydantic prints `**********` for it, including in the repr of
    # the whole Settings object. app/rerank.py copies it into HF_TOKEN, which is
    # what huggingface_hub actually reads.
    hugging_face: SecretStr | None = None

    # When the filters cannot fill a page, widen them one at a time rather than
    # returning almost nothing. The ladder and the never-relax list live in
    # app/relax.py; this only decides whether the loop runs at all.
    #
    # On by default because an empty page is worse than a widened one, and OFF
    # in run_eval because recall must be measured against a FIXED filter set -
    # a harness that quietly widens when a query returns little would report the
    # relaxation as retrieval quality.
    relax_filters: bool = True

    # Rows that must pass the filters before relaxation stops. None means "the
    # requested limit", which is deliberately conservative: relaxing while the
    # filters can still fill the page overrides a constraint the user actually
    # stated. Raising it trades filter fidelity for ranking quality, because
    # ranking 12 survivors is the filter choosing the results, not the vector.
    relax_target_rows: int | None = None

    # How many requests /api/stats keeps per endpoint. A ring buffer in memory,
    # so this is the whole memory cost and it does not grow with uptime.
    #
    # It is a count of REQUESTS, not a period of time, which is why the endpoint
    # returns it: "p95 over the last 500 requests" is a claim somebody can check
    # and "p95" on its own is not.
    stats_window: int = 500

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
