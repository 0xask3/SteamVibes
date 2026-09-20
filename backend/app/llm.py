"""Ollama chat client, constrained to a JSON schema.

Same shape as app/embedding.py: one long-lived httpx.Client and keep_alive.
`format` constrains generation to the schema, which makes malformed JSON
nearly impossible - but schema-valid nonsense is still nonsense, so the
caller's fallback stays.
"""

import json
from typing import Any

import httpx

from app.config import settings

# Generous: a 14b model on a cold load can take ~30s before it emits a token.
_REQUEST_TIMEOUT = 180.0

_client = httpx.Client(base_url=settings.ollama_base_url, timeout=_REQUEST_TIMEOUT)


def chat_json(
    system: str,
    user: str,
    schema: dict[str, Any],
    model: str | None = None,
    think: bool | None = None,
) -> dict[str, Any]:
    """One chat turn, returning parsed JSON matching `schema`.

    Raises on transport failure, a non-2xx response or unparseable content;
    callers decide what to do, and app/query_parser.py degrades.
    """
    payload: dict[str, Any] = {
        "model": model or settings.chat_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": schema,
        "stream": False,
        "keep_alive": settings.ollama_keep_alive,
        "options": {
            # A parser that varies run to run cannot be evaluated.
            "temperature": 0,
            # Must hold the 452-tag vocabulary. See settings.chat_num_ctx.
            "num_ctx": settings.chat_num_ctx,
        },
    }

    # Sent only when set: Ollama 400s on `think` for a model predating it, so
    # the key must be ABSENT rather than false.
    effective_think = settings.chat_think if think is None else think
    if effective_think is not None:
        payload["think"] = effective_think

    response = _client.post("/api/chat", json=payload)
    response.raise_for_status()

    content = response.json()["message"]["content"]
    parsed: dict[str, Any] = json.loads(content)
    return parsed
