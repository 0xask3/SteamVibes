"""Cross-encoder reranking over the candidate pool, in-process on the host GPU.

A bi-encoder never reads query and document together; a cross-encoder scores
the pair, too slowly for 130,651 games and fast enough for the 200 stage 1
picked - where all 9 tail misses and 9 of 11 specific misses already sit.

In-process because both serving routes are dead here: Ollama has no rerank
endpoint (404, PR #7219 open since 2024), and TEI cannot reach the GPU through
Docker Desktop's WSL2 backend, where the CUDA driver API answers
CUDA_ERROR_NO_DEVICE and it starts on CPU with only a warning. See NOTES.md
2026-09-07. The cost: the containerised backend cannot rerank at all, so
docker-compose pins RANK_METHOD=rrf.

The failure contract is the parser's - a model that will not load degrades to
the SQL ordering with a WARNING, never a 500. `RerankUnavailable` carries the
reason and app/search.py is the only place that catches it.
"""

import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# Per-model input templates, the reranker's answer to app/embedding.py's
# _MODEL_PREFIXES. A model trained on a wrapped prompt returns near-zero logits
# on a bare pair, and the failure is QUIET - it still orders an easy triple
# correctly, just without conviction, and scored 8.1% overall. failures.md #37.
_QWEN3_SYSTEM = (
    "<|im_start|>system\n"
    "Judge whether the Document meets the requirements based on the Query and "
    'the Instruct provided. Note that the answer can only be "yes" or "no".'
    "<|im_end|>\n<|im_start|>user\n"
)
_QWEN3_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def _qwen3_pair(query: str, document: str) -> tuple[str, str]:
    """Qwen3-Reranker's chat template, split across the CrossEncoder pair.

    Both halves are load-bearing: the yes/no logit the model was trained to
    emit lands at exactly that assistant preamble.
    """
    return (
        f"{_QWEN3_SYSTEM}<Instruct>: {settings.rerank_instruction}\n<Query>: {query}\n",
        f"<Document>: {document}{_QWEN3_SUFFIX}",
    )


# Matched as a FAMILY substring, unlike app/embedding.py's exact-name rule:
# every Qwen3-Reranker shares one template across sizes, conversions and
# re-uploads, so exact matching would silently mis-invoke all of them.
_TEMPLATES: tuple[tuple[str, Callable[[str, str], tuple[str, str]]], ...] = (
    ("qwen3-reranker", _qwen3_pair),
)


def _pair_for(model: str, query: str, document: str) -> tuple[str, str]:
    """Wrap one (query, document) pair the way this model expects it.

    The default is the raw pair, which is what bge and the classic
    cross-encoders want.
    """
    lowered = model.lower()
    for family, formatter in _TEMPLATES:
        if family in lowered:
            return formatter(query, document)
    return query, document


# Lazily, so importing this module costs nothing for paths that never rerank;
# behind a lock because `def` endpoints run in a threadpool, so two concurrent
# first requests would otherwise both load it.
_model: Any = None
_model_name: str | None = None
_load_lock = threading.Lock()
_load_failed: str | None = None


class RerankUnavailable(RuntimeError):
    """The cross-encoder could not score this pool. Callers fall back."""


def _load() -> Any:
    """Return the loaded CrossEncoder, raising RerankUnavailable if it will not.

    A failed load is remembered: retrying a multi-second import-and-download on
    every search turns a quality regression into a latency outage.
    """
    global _model, _model_name, _load_failed

    if _model is not None and _model_name == settings.rerank_model:
        return _model
    if _load_failed is not None and _model_name == settings.rerank_model:
        raise RerankUnavailable(_load_failed)

    with _load_lock:
        # Re-check inside the lock: another thread may have loaded it.
        if _model is not None and _model_name == settings.rerank_model:
            return _model
        # Into the environment, not an argument: every download path inside
        # huggingface_hub reads HF_TOKEN. setdefault, so an exported token wins.
        if settings.hugging_face is not None:
            os.environ.setdefault("HF_TOKEN", settings.hugging_face.get_secret_value())

        try:
            # Imported here, not at module scope: torch costs seconds and pulls
            # ~2.5GB of CUDA libraries into the process, and nothing that does
            # not rerank should pay that - including every ingest script.
            from sentence_transformers import CrossEncoder

            model = CrossEncoder(
                settings.rerank_model,
                device=settings.rerank_device,
                # Runs code from the model repo; off unless asked for.
                trust_remote_code=settings.rerank_trust_remote_code,
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

        # Warm at FULL POOL SIZE: the first batch of a given shape pays CUDA
        # kernel selection (9,668ms against a 963ms steady state), and a 2-pair
        # probe does not trigger the same kernels.
        try:
            filler = ["warmup document"] * settings.rerank_candidates
            model.predict(
                [_pair_for(settings.rerank_model, "warmup", d) for d in filler],
                batch_size=settings.rerank_batch_size,
                show_progress_bar=False,
            )
        except Exception as exc:  # noqa: BLE001 - a slow first query, not a failure
            logger.warning(
                "reranker warm-up failed, first search will be slow: %s", exc
            )

        logger.info(
            "cross-encoder %s loaded on %s",
            settings.rerank_model,
            settings.rerank_device,
        )
        return _model


def rerank_scores(query: str, documents: list[str]) -> list[float]:
    """Score every document against the query, in INPUT ORDER.

    Order is the contract: app/search.py pairs these positionally with its
    rows, so sorting here would attach every score to the wrong game.
    """
    if not documents:
        return []

    model = _load()
    try:
        scores = model.predict(
            [_pair_for(settings.rerank_model, query, doc) for doc in documents],
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

    Eager, and only in `run_eval`: degrading is right for a user and wrong for
    a measurement, which would report the BASELINE ordering under this model's
    label. Better to refuse to start.
    """
    try:
        model = _load()
    except RerankUnavailable as exc:
        raise RuntimeError(
            f"RANK_METHOD=rerank but the cross-encoder would not load: {exc}"
        ) from exc

    # LOADING IS NOT SCORING: a model can load cleanly and raise on every
    # predict(), which printed a table byte-identical to the baseline and read
    # as "no better" rather than "never ran". failures.md #36.
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

    # Constant scores are the other silent failure: `_ranks` is stable, so ties
    # keep cosine order and the result reproduces the baseline exactly.
    if len(set(scores)) < len(scores):
        raise RuntimeError(
            f"{settings.rerank_model} returned identical scores {scores} for two "
            "obviously different documents. A constant scorer cannot reorder "
            "anything, and the result would silently be the baseline ordering."
        )

    device = getattr(model, "device", "unknown")
    if settings.rerank_device == "cuda" and "cuda" not in str(device).lower():
        # Loud, because sentence-transformers falls back to CPU silently and a
        # CPU eval would run for hours.
        raise RuntimeError(
            f"RERANK_DEVICE=cuda but {settings.rerank_model} loaded on "
            f"{device!r}. Check torch: a PyPI wheel on Windows is CPU-only and "
            "reports torch.cuda.is_available() == False with no other symptom."
        )
