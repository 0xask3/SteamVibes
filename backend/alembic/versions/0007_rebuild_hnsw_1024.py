"""Rebuild the HNSW index after 0006's re-dimension and the re-embed.

0006 dropped it and cleared every vector. This puts it back once the embed job
has refilled the column at the new width.

    uv run alembic upgrade 0006
    uv run python -m ingest.embed_all --reload
    uv run alembic upgrade head                  # this migration

Same definition as 0003 and 0005, over a wider column, and vector_cosine_ops
must still match the <=> operator in app/search.py.

Do not rebuild between model comparisons: the eval wants the exact scan, which
is ground truth rather than an approximation.

1020MB, not the ~680MB a 510MB x 1024/768 ratio predicts - size is a step
function of dimension, because at 1024 dims an element is ~4.4KB and only one
fits an 8KB page (NOTES.md 2026-09-04). Needs shm_size: 4gb on the db service
or the parallel build fails with "No space left on device".

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-04

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64


def upgrade() -> None:
    op.execute("SET maintenance_work_mem = '2GB'")
    op.execute("SET max_parallel_maintenance_workers = 4")
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ix_games_embedding_hnsw
            ON games
         USING hnsw (embedding vector_cosine_ops)
          WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_games_embedding_hnsw")
