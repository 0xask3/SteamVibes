"""Add required_age and a denormalised tags array, dropping HNSW first.

Two columns Weekend 2 needs.

required_age was in the source JSON and omitted from 0001. It is what makes
"safe for a 7 year old" answerable - that query currently returns games tagged
Violent and Nudity. Only a partial signal: 137,643 of 138,964 rows are 0
because Steam sets it only where legally required, so age filtering must also
exclude mature tags. That is what the array is for.

games.tags duplicates game_tags deliberately. game_tags stays the source of
truth because it carries votes, which "top 15 by votes" depends on. The array
exists because tag filtering is the wrong shape for a normalised table:

    -- normalised: join, group, count
    WHERE app_id IN (SELECT app_id FROM game_tags WHERE tag = ANY(:tags)
                     GROUP BY app_id HAVING count(*) = 2)

    -- array + GIN: one operator
    WHERE tags @> ARRAY['Co-op', 'Base Building']

Both are kept in step by ingest/load_games.py, which writes them from the same
source dict.

NOT NULL DEFAULT '{}' rather than nullable: NULL breaks exclusion filters,
because NOT (NULL && ARRAY['Violent']) is NULL rather than true, so an untagged
game would be wrongly dropped from "no violent games".

WHY THIS DROPS THE HNSW INDEX
-----------------------------
The backfill updates all 138,964 rows. Every update writes a new row version,
and adding ~200 bytes per row fills pages, so most updates cannot stay HOT.
Non-HOT updates require a new entry in *every* index on the table - including
the 513MB HNSW graph over 130,651 vectors. HNSW inserts are expensive by
design, which is exactly why 0003 built that index after the embed job rather
than before it.

Measured: with HNSW present the backfill was still running after several
minutes. Dropping it first makes the same UPDATE cheap.

The index is NOT recreated here. required_age cannot be backfilled from the
database - the value lives only in games.json - so a `load_games --reload` has
to follow this migration, and that reload updates every row again. Rebuilding
HNSW before that would pay the same cost twice.

    uv run alembic upgrade 0004
    uv run python -m ingest.load_games --reload
    uv run alembic upgrade head          # 0005 rebuilds HNSW

Search still works between the two, just without the index. Same shape as
Weekend 1: load everything, then build the vector index once.

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
