"""Rebuild the HNSW index after 0006's re-dimension and the re-embed.

0006 dropped ix_games_embedding_hnsw and cleared every vector. This puts the
index back, once the embed job has refilled the column at the new width.

Run order:

    uv run alembic upgrade 0006
    uv run python -m ingest.embed_all --reload
    uv run alembic upgrade head                  # this migration

Same definition as 0003 and 0005, over a wider column. vector_cosine_ops must
match the <=> operator in app/search.py; a mismatch does not error, it silently
disables the index and falls back to scanning every row.

Do not run this between model comparisons. The eval wants an exact scan - it is
ground truth rather than an approximation, and it removes index recall as a
variable - and building this three times costs far more than the queries save.

Bigger than its 768-dim predecessor, and by more than the ratio suggests: this
docstring predicted ~680MB (510MB scaled by 1024/768) and the build came out at
1020MB - size is a step function of dimension, not a ratio, because at 1024 dims
an element is ~4.4KB and only one fits an 8KB page. See NOTES.md 2026-09-04.
Still needs shm_size: 4gb on the db service or the parallel build fails with
"No space left on device" - see NOTES.md 2026-08-26.

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
