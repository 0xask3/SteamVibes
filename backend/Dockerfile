# Backend: FastAPI over uvicorn, dependencies installed with uv from uv.lock.
#
# Ollama is NOT in this image and is not reachable at 127.0.0.1 from inside it.
# The container talks to the host's Ollama through host.docker.internal, which
# docker-compose.yml sets. That is deliberate: embedding 130k games needs the
# GPU, and the GPU is on the host.

FROM python:3.14-slim AS base

# Pinned rather than :latest - the lockfile is only reproducible if the tool
# reading it is too.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies first, as their own layer: they change far less often than the
# source, so editing app/search.py does not reinstall SQLAlchemy.
COPY pyproject.toml uv.lock ./
# --no-default-groups, not --no-dev: it also leaves out the `rerank` group, which
# is torch, triton and ~2.9GB of nvidia-* CUDA wheels this container cannot use -
# compose pins RANK_METHOD=rrf because the GPU is unreachable from inside it.
# Before this, --no-dev installed all of it and most of the first build was
# spent downloading it. See the comment on the group in pyproject.toml.
RUN uv sync --frozen --no-default-groups

COPY . .

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# Migrations before the server. `alembic upgrade head` is idempotent and this is
# a single-replica setup, so there is no concurrent-migration race to worry
# about. It is also the only way `docker compose up` reaches a working API on a
# clean volume - the alternative is a README step that everyone forgets.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
