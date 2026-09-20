"""Widen games.embedding to vector(1024) for a multilingual model.

Widens the column once so every 1024-dim model - arctic, bge-m3,
qwen3-embedding - is a .env line plus `embed_all --reload` rather than another
migration. 1024 is also inside pgvector's HNSW limit of 2,000.

    uv run alembic upgrade 0006
    uv run python -m ingest.embed_all --reload
    uv run alembic upgrade head                   # 0007 rebuilds HNSW

The index is dropped FIRST, per the rule in CLAUDE.md: ALTER COLUMN TYPE
rewrites every row, and each one would need a new entry in the graph. Search
still works in between, as an exact scan.

THE VECTORS ARE DISCARDED, and the downgrade is LOSSY - it restores the width,
not the data, so coming back means re-embedding. 768 components cannot be
widened to 1024, and a vector from another model is not comparable anyway.
The downgrade also leaves HNSW dropped, because every vector is NULL at that
point; rebuild with `downgrade 0004 && upgrade 0005` once vectors exist.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-04

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in step with EMBEDDING_DIM in app/models.py.
NEW_DIM = 1024
OLD_DIM = 768


def _redimension(width: int) -> None:
    # ix_games_unembedded stays: it is tiny, and it is what makes the embed job
    # resumable. After this it covers every row, which is correct.
    op.execute("DROP INDEX IF EXISTS ix_games_embedding_hnsw")
    op.execute(
        f"ALTER TABLE games ALTER COLUMN embedding TYPE vector({width}) "
        f"USING NULL::vector({width})"
    )
    op.execute("UPDATE games SET embedding_model = NULL, embedded_at = NULL")


def upgrade() -> None:
    _redimension(NEW_DIM)


def downgrade() -> None:
    _redimension(OLD_DIM)
