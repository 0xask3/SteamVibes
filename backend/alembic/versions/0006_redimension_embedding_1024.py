"""Widen games.embedding to vector(1024) for a multilingual model.

WHY
---
Weekend 3's eval harness put a number on what `nomic-embed-text` delivers:
recall@10 of 6.1% overall - 9.2% English, 0.0% German. The German zero looks
like a language problem, but the German queries are near-parallel to the English
ones ("Städtebau-Simulation" and "city builder" expect the same games), so DE's
ceiling is EN's 9.2%. Both numbers have the same cause: nomic-embed-text is an
English-only 2024 model.

Every candidate replacement worth measuring - snowflake-arctic-embed2, bge-m3,
qwen3-embedding:0.6b - emits 1024 dimensions. So this migration is not "switch
to model X". It widens the column once, after which comparing those three is
`EMBED_MODEL=` plus `embed_all --reload`, with no further schema work.

1024 is also inside pgvector's limits, which the leaderboard leaders are not:
HNSW indexes at most 2,000 dimensions for `vector` (4,000 for `halfvec`), and
qwen3-embedding's 4b/8b emit 2560/4096.

WHY THE INDEX GOES FIRST
------------------------
Same reason as 0004, and the rule is in CLAUDE.md: a full-table write with
ix_games_embedding_hnsw present forces a new entry in a 510MB graph for every
row touched. ALTER COLUMN TYPE rewrites all 138,964 rows, so the index is
dropped before, and 0007 rebuilds it after the embed run - not before.

    uv run alembic upgrade 0006
    uv run python -m ingest.embed_all --reload    # sets EMBED_MODEL's vectors
    uv run alembic upgrade head                   # 0007 rebuilds HNSW

Search works in between, just without the index - an exact scan, which is what
the model comparison wants anyway.

WHY THE VECTORS ARE DISCARDED
-----------------------------
768-dim vectors cannot be widened to 1024 - there is no meaningful value for the
new components, and a vector from a different model is not comparable regardless.
`USING NULL::vector(1024)` clears them as part of the rewrite Postgres is already
doing, rather than paying for a separate full-table UPDATE first.

embedding_model and embedded_at are cleared alongside, so check_model_consistency()
in ingest/embed_all.py starts from a clean slate and the `embedding IS NULL` work
queue covers every row.

DOWNGRADE IS LOSSY. It restores the column width, not the data. Coming back to
768 means re-running the embed job with nomic-embed-text (~12 min). There is no
path that returns the original vectors.

The downgrade also leaves HNSW dropped, so `downgrade 0005` lands on 768 dims
with no index even though 0005 is the migration that builds one. Deliberate:
every vector is NULL at that point, so the index would describe nothing, and the
re-embed that has to follow would need it dropped again anyway. Rebuild with
`downgrade 0004 && upgrade 0005` once vectors exist. Verified by round trip on a
scratch database: 0001->0007, down to 0005, back to head, down to 0006, back to
head - column width and index presence correct at every stop.

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

# Keep in step with EMBEDDING_DIM in app/models.py. app/embedding.py checks
# every response against it and exits rather than writing a wrong-width vector.
NEW_DIM = 1024
OLD_DIM = 768


def _redimension(width: int) -> None:
    # ix_games_unembedded is deliberately left in place: it is a partial index
    # on `embedding IS NULL`, it is tiny, and it is what makes the embed job
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
