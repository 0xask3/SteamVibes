"""Add required_age and a denormalised tags array, dropping HNSW first.

required_age makes "safe for a 7 year old" answerable, but only partially:
Steam sets it where legally required, so 137,643 of 138,964 rows are 0 and age
filtering must also exclude mature tags. That is what the array is for.

games.tags duplicates game_tags deliberately - that table keeps the votes "top
15 by votes" needs, while the array makes tag filtering one GIN operator
instead of a join and GROUP BY. load_games.py writes both from the same dict.
NOT NULL DEFAULT '{}', because NOT (NULL && ARRAY['Violent']) is NULL rather
than true and would drop untagged games from "no violent games".

DROPS HNSW FIRST: the backfill updates every row, each one needing a new entry
in the graph, which left it still running after several minutes. It is not
rebuilt here either, because required_age lives only in games.json, so a
reload has to follow and would pay the cost twice.

    uv run alembic upgrade 0004
    uv run python -m ingest.load_games --reload
    uv run alembic upgrade head          # 0005 rebuilds HNSW

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-29

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept identical to 0003 so the downgrade restores exactly what was there.
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64


def upgrade() -> None:
    # Before any bulk write. See the module docstring.
    op.execute("DROP INDEX IF EXISTS ix_games_embedding_hnsw")

    op.add_column(
        "games",
        sa.Column(
            "required_age", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "games",
        sa.Column(
            "tags",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )

    # One grouped pass, not a correlated subquery per game: ix_game_tags_app_votes
    # covers (app_id, votes) and not `tag`, so the correlated form costs a heap
    # visit for each of the ~14 tags on each of the 138,964 games.
    # COALESCE because a game with no tags produces no row in the aggregate.
    op.execute(
        """
        UPDATE games g
           SET tags = COALESCE(agg.tags, '{}'::text[])
          FROM (SELECT app_id, array_agg(tag ORDER BY votes DESC) AS tags
                  FROM game_tags
                 GROUP BY app_id) agg
         WHERE agg.app_id = g.app_id
        """
    )

    op.create_index("ix_games_tags_gin", "games", ["tags"], postgresql_using="gin")

    # No index on required_age on purpose: 99% of rows are 0, so a btree would
    # be ignored for the only query that matters (required_age <= :max_age).


def downgrade() -> None:
    op.drop_index("ix_games_tags_gin", table_name="games")
    op.drop_column("games", "tags")
    op.drop_column("games", "required_age")

    # Restore 0003's end state, which included the HNSW index.
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
