"""Embed every game that has embed_text but no vector yet.

Resumable by construction: the work queue is `embedding IS NULL`, which is a
question the database answers directly (and cheaply, via ix_games_unembedded).
Interrupt it and re-run; it continues where it stopped.

Throughput on an RTX 4080 SUPER is flat at ~172-183 embeddings/sec for every
batch size from 8 to 128, so anything in that range performs the same: the GPU
is the bottleneck, not the HTTP round trip. 256 is rejected by Ollama with a
400. Re-run ingest/bench_embed.py after changing models.
"""

import argparse
from datetime import UTC, datetime

from sqlalchemy import distinct, func, select, update
from tqdm import tqdm

from app.config import settings
from app.db import session_scope
from app.embedding import embed_texts
from app.models import EMBEDDING_DIM, Game

BATCH_SIZE = 128
# Rows pulled from the database per round trip. Independent of BATCH_SIZE: this
# is a database read size, that is an Ollama request size.
PAGE_SIZE = 1024


def count_pending() -> int:
    with session_scope() as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(Game)
                .where(Game.embed_text.isnot(None), Game.embedding.is_(None))
            )
            or 0
        )


def check_model_consistency(allow_mixed: bool) -> None:
    """Refuse to mix vectors from different models in one column.

    Distances between vectors from different models are meaningless, and
    nothing downstream would report an error — search would just quietly get
    worse. Weekend 3's bge-m3 swap re-embeds everything, so this should only
    ever fire on a mistake.
    """
    with session_scope() as session:
        # The isnot(None) filters in SQL; the comprehension narrows the type.
        models = {
            model
            for model in session.scalars(
                select(distinct(Game.embedding_model)).where(
                    Game.embedding_model.isnot(None)
                )
            ).all()
            if model is not None
        }

    unexpected = models - {settings.embed_model}
    if unexpected and not allow_mixed:
        raise SystemExit(
            f"database already holds vectors from {sorted(unexpected)}, but "
            f"EMBED_MODEL is {settings.embed_model!r}.\n"
            "Mixing models in one column makes distances meaningless. Either "
            "re-embed everything, or pass --allow-mixed if you know why you "
            "want this."
        )


def write_vectors(rows: list[dict[str, object]]) -> None:
    """One executemany UPDATE for the whole page.

    Uses SQLAlchemy's ORM bulk-update-by-primary-key form: pass `update(Game)`
    with no WHERE, plus dicts keyed by real column names. The ORM builds
    `WHERE app_id = ...` from the primary key itself. Supplying our own WHERE
    and bindparams instead puts it on a different code path that rejects
    executemany.

    synchronize_session=False because this session holds no Game objects to
    reconcile — the reader selects columns, not entities.
    """
    if not rows:
        return
    with session_scope() as session:
        session.execute(
            update(Game), rows, execution_options={"synchronize_session": False}
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed games that have no vector.")
    parser.add_argument(
        "--limit", type=int, default=None, help="Stop after N embeddings."
    )
    parser.add_argument(
        "--batch-size", type=int, default=BATCH_SIZE, help=f"Default {BATCH_SIZE}."
    )
    parser.add_argument(
        "--allow-mixed",
        action="store_true",
        help="Permit vectors from a different model than those already stored.",
    )
    args = parser.parse_args()

    check_model_consistency(args.allow_mixed)

    pending = count_pending()
    if args.limit is not None:
        pending = min(pending, args.limit)
    if pending == 0:
        print("nothing to embed.")
        return

    print(f"model:      {settings.embed_model} ({EMBEDDING_DIM} dims)")
    print(f"endpoint:   {settings.ollama_base_url}")
    print(f"batch size: {args.batch_size}")
    print(f"pending:    {pending:,}\n")

    done = 0
    with tqdm(
        total=pending, unit="game", desc="embedding", smoothing=0.05
    ) as progress:
        while done < pending:
            # Re-query rather than paginate by offset: embedded rows drop out of
            # this filter, so the next page is always the next unembedded rows.
            with session_scope() as session:
                page = session.execute(
                    select(Game.app_id, Game.embed_text)
                    .where(Game.embed_text.isnot(None), Game.embedding.is_(None))
                    .order_by(Game.app_id)
                    .limit(min(PAGE_SIZE, pending - done))
                ).all()

            if not page:
                break

            updates: list[dict[str, object]] = []
            now = datetime.now(UTC)
            for start in range(0, len(page), args.batch_size):
                chunk = page[start : start + args.batch_size]
                vectors = embed_texts([row.embed_text for row in chunk])
                updates.extend(
                    {
                        # Keys must be the actual column names, app_id included:
                        # that is what the ORM builds the WHERE clause from.
                        "app_id": row.app_id,
                        "embedding": vector,
                        "embedding_model": settings.embed_model,
                        "embedded_at": now,
                    }
                    for row, vector in zip(chunk, vectors, strict=True)
                )

            # Commit per page, so an interrupt costs at most PAGE_SIZE games.
            write_vectors(updates)
            done += len(page)
            progress.update(len(page))

    remaining = count_pending()
    print(f"\nembedded {done:,}   still pending: {remaining:,}")


if __name__ == "__main__":
    main()
