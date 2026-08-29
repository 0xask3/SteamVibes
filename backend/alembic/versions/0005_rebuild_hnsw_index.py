"""Rebuild the HNSW index after 0004's bulk write and the reload that follows.

0004 dropped ix_games_embedding_hnsw because a full-table UPDATE forces a new
entry in every index per row, and HNSW inserts are expensive. This puts it back
once the writes are finished.

Run order:

    uv run alembic upgrade 0004
    uv run python -m ingest.load_games --reload      # populates required_age
    uv run alembic upgrade head                      # this migration

Identical definition to 0003. vector_cosine_ops must match the <=> operator in
app/search.py; a mismatch does not error, it silently disables the index and
falls back to scanning every row.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-29

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64


def upgrade() -> None:
    # Needs shm_size: 4gb on the db service, or the parallel build fails with
    # "No space left on device" - see NOTES.md 2026-08-26.
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
