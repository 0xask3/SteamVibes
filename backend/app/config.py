"""Typed settings, read once from the repo-root .env.

Anything missing raises at import time rather than halfway through a 35-minute
embedding run.
"""

from functools import lru_cache
from pathlib import Path

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
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768

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
    # anything with. None omits the flag entirely, which is required for models
    # that predate it - Ollama rejects `think` outright for qwen2.5.
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so the .env is parsed once per process."""
    return Settings()  # type: ignore[call-arg]  # values come from the env file


settings = get_settings()
