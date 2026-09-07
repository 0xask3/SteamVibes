"""Cross-encoder reranking over the candidate pool, in-process on the host GPU.

Why a cross-encoder. A bi-encoder embeds the query and the document separately
and compares vectors, so it never reads them together - it cannot tell that
"co-op" names a mode rather than a word. A cross-encoder scores the pair. It is
far too slow to run over 130,651 games, which is exactly why it runs over the
200 that stage 1 already picked. Measured justification: of the 76 labelled
targets not already in the top 10, ALL 9 tail misses and 9 of 11 specific misses
are inside that pool - found by retrieval and buried by the ordering.

Why in-process rather than a model server, which is the shape every other model
in this project has. Two serving routes were tried first and both are dead ends
on this machine:

  Ollama has no rerank endpoint at all. `POST /api/rerank` is a 404 as of
  0.33.3 and PR #7219 has been open since 2024, so BUILD_PLAN's
  `ollama pull bge-reranker-v2-m3` cannot work. Every community workaround
  scores through the EMBEDDING endpoint, which is the bi-encoder we already have.

  Hugging Face TEI serves rerankers properly, but not here: Docker Desktop's
  WSL2 backend hands a container working NVML - `nvidia-smi` lists the 4080 by
  UUID - alongside a CUDA driver API that answers CUDA_ERROR_NO_DEVICE, so TEI
  starts on CPU with a warning rather than an error and never finishes warming
  up. It is a stale user-mode driver in Docker's own managed WSL distro
  (615.65.06 against a 616.56 kernel driver), and it tracks Docker Desktop's
  version rather than the host's NVIDIA driver, so upgrading the driver and
  restarting WSL does not move it. Reproduced with a plain `docker run --gpus
  all` on the same image, so it is not the compose file. See NOTES.md.

The host GPU works fine - Ollama has been using it all along - so the model
loads here. BUILD_PLAN sanctions this explicitly: "or run it via
sentence-transformers". It is a model runtime, not an agent framework, so it
does not touch CLAUDE.md's actual prohibition. The cost is real and worth naming:
the containerised backend cannot rerank, because torch is not in that image and
the GPU is not either. That is the same honest limitation ingest already has.

The failure contract is the parser's. Reranking improves an ordering that
already works, so a model that will not load must degrade to the SQL ordering
with a WARNING, never a 500. `RerankUnavailable` carries the reason and
app/search.py is the only place that catches it.
"""

import logging
import os
import threading
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# Loaded once, lazily, behind a lock. Lazily because importing this module must
# not cost a model load for the CLI paths and evals that never rerank; behind a
# lock because FastAPI endpoints are plain `def` and therefore run in a
# threadpool, so two concurrent first-requests would otherwise both load it.
_model: Any = None
_model_name: str | None = None
_load_lock = threading.Lock()
_load_failed: str | None = None


class RerankUnavailable(RuntimeError):
    """The cross-encoder could not score this pool. Callers fall back."""


def _load() -> Any:
    """Return the loaded CrossEncoder, raising RerankUnavailable if it will not.

    A failed load is remembered. Without that, every search would re-attempt a
    multi-second import-and-download that has already failed once, turning a
    quality regression into a latency outage - and the WARNING would repeat per
    request instead of per process.
    """
    global _model, _model_name, _load_failed

    if _model is not None and _model_name == settings.rerank_model:
        return _model
    if _load_failed is not None and _model_name == settings.rerank_model:
        raise RerankUnavailable(_load_failed)

    with _load_lock:
        # Re-check inside the lock: another thread may have loaded it while this
        # one waited.
        if _model is not None and _model_name == settings.rerank_model:
            return _model
        # Copied into the environment rather than passed as an argument: every
        # download path inside huggingface_hub reads HF_TOKEN, including the ones
        # transformers reaches through config and tokenizer loading. setdefault,
        # so a token already exported in the shell still wins.
        if settings.hugging_face is not None:
            os.environ.setdefault("HF_TOKEN", settings.hugging_face.get_secret_value())

        try:
            # Imported here, not at module scope: torch costs seconds to import
            # and pulls ~2.5GB of CUDA libraries into the process. Nothing that
            # does not rerank should pay that, including `run_eval` at the
            # default RANK_METHOD and every ingest script.
            from sentence_transformers import CrossEncoder

            model = CrossEncoder(
                settings.rerank_model,
                device=settings.rerank_device,
                # Runs code from the model repo. Off unless asked for; see the
                # setting's comment in app/config.py.
                trust_remote_code=settings.rerank_trust_remote_code,
                # fp16 halves the weights and roughly doubles throughput on Ada.
                # Ranking is an ORDERING, so the last bits of score precision do
                # not survive into the output anyway.
                model_kwargs={"torch_dtype": "float16"}
                if settings.rerank_device == "cuda"
                else {},
            )
        except Exception as exc:
            _load_failed = f"{settings.rerank_model}: {type(exc).__name__}: {exc}"
            _model_name = settings.rerank_model
            raise RerankUnavailable(_load_failed) from exc

        _model, _model_name, _load_failed = model, settings.rerank_model, None
        logger.info(
            "cross-encoder %s loaded on %s",
            settings.rerank_model,
            settings.rerank_device,
        )
        return _model


def rerank_scores(query: str, documents: list[str]) -> list[float]:
    """Score every document against the query, in input order.

    Order is the contract: app/search.py pairs these positionally with the rows
    it fetched, so returning them sorted would silently attach every score to
    the wrong game - which reads as a bad model rather than a bug.
    """
    if not documents:
        return []

    model = _load()
    try:
        scores = model.predict(
            [(query, doc) for doc in documents],
            batch_size=settings.rerank_batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
    except Exception as exc:
        raise RerankUnavailable(
            f"{settings.rerank_model} failed on {len(documents)} pairs: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return [float(s) for s in scores]


def verify_rerank_model() -> None:
    """Load the model before an eval measures anything with it.

    Deliberately eager, and only in `run_eval`. The search path degrades on a
    failed load, which is right for a user and wrong for a measurement: a run
    that silently fell back would report the BASELINE ordering as a reranker
    result, which is the same invisible-mismatch failure as EMBED_MODEL
    disagreeing with games.embedding_model. Better to refuse to start.
    """
    try:
        model = _load()
    except RerankUnavailable as exc:
        raise RuntimeError(
            f"RANK_METHOD=rerank but the cross-encoder would not load: {exc}"
        ) from exc

    # Loading is not scoring, and that distinction cost a whole eval run.
    # gte-multilingual-reranker-base loads cleanly and then raises a CUDA
    # device-side assert on every predict(), so all 118 queries degraded to the
    # SQL ordering and the harness printed a table byte-identical to the
    # baseline - which reads as "this model is no better" rather than "this
    # model never ran". Smoke-test with a pair whose ordering is not in doubt.
    probe = [
        "Cities: Skylines II. A city building simulator. Tags: City Builder, Simulation.",
        "Barbie Dreamhouse Adventures. Decorate rooms and style outfits. Tags: Casual.",
    ]
    try:
        scores = rerank_scores("city builder", probe)
    except RerankUnavailable as exc:
        raise RuntimeError(
            f"RANK_METHOD=rerank but {settings.rerank_model} loaded and then "
            f"failed to score: {exc}"
        ) from exc

    # Constant scores are the other silent failure: `_ranks` is a stable sort, so
    # every tie keeps the incoming cosine order and the fused result reproduces
    # the baseline exactly. Indistinguishable from a model that has no opinion.
    if len(set(scores)) < len(scores):
        raise RuntimeError(
            f"{settings.rerank_model} returned identical scores {scores} for two "
            "obviously different documents. A constant scorer cannot reorder "
            "anything, and the result would silently be the baseline ordering."
        )

    device = getattr(model, "device", "unknown")
    if settings.rerank_device == "cuda" and "cuda" not in str(device).lower():
        # CPU is not a slower version of this measurement, it is an unusable
        # one - TEI on CPU never finished warming up, and a full eval would be
        # hours. Loud, because sentence-transformers falls back silently.
        raise RuntimeError(
            f"RERANK_DEVICE=cuda but {settings.rerank_model} loaded on "
            f"{device!r}. Check torch: a PyPI wheel on Windows is CPU-only and "
            "reports torch.cuda.is_available() == False with no other symptom."
        )
