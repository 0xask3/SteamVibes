"""Add the HNSW index on games.embedding.

Separate from 0001 and run only after ingest/embed_all.py finishes: building
the graph incrementally means rewiring it 130k times.

`vector_cosine_ops` must match the `<=>` operator used by search. A mismatch
does not error - Postgres silently ignores the index and scans every row, so
results stay correct and searches are ~100x slower with nothing to show why.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-26

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# pgvector defaults: m=16, ef_construction=64. Stated explicitly so a future
# change is visible in the diff rather than inherited silently.
#   m               - connections per node. Higher = better recall, bigger index.
#   ef_construction - candidates considered while building. Higher = better
#                     graph, slower build. Neither affects query time directly.
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64


def upgrade() -> None:
    # Default maintenance_work_mem is 64MB. HNSW builds far faster when the
    # graph fits in memory; below that threshold it spills and crawls.
    op.execute("SET maintenance_work_mem = '2GB'")
    op.execute("SET max_parallel_maintenance_workers = 4")

    op.execute(
        f"""
        CREATE INDEX ix_games_embedding_hnsw
            ON games
         USING hnsw (embedding vector_cosine_ops)
          WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_games_embedding_hnsw")
