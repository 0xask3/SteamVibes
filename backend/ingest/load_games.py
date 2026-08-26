"""Load data/games.json into Postgres.

Idempotent and resumable, per CLAUDE.md. Re-running skips games already
present; --reload forces a full upsert pass. An interrupt costs at most one
batch, never the whole run.

embed_text is built here rather than in the embed job: the tags dict is
already in memory at this point, so picking the top 15 by votes is free.
Doing it later would mean a second traversal of 1.18M tag rows.
"""

import argparse
import json
from collections.abc import Iterator
from datetime import date
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from tqdm import tqdm

from app.config import REPO_ROOT
from app.db import session_scope
from app.models import Game, GameCategory, GameGenre, GameTag

DATA_PATH = REPO_ROOT / "data" / "games.json"
BATCH_SIZE = 1000
TOP_TAGS_FOR_EMBEDDING = 15

# Explicit rather than strptime("%b"): month abbreviations are locale-dependent
# and this must not change behaviour on a differently-configured machine.
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Never overwritten on re-run. Clobbering these would discard ~35 minutes of
# embedding work every time the loader runs again. total_reviews is generated
# by Postgres and cannot be written at all.
PRESERVE_ON_CONFLICT = {
    "app_id",
    "total_reviews",
    "list_price_usd",  # also generated; writing to it is an error
    "embedding",
    "embedding_model",
    "embedded_at",
}


def parse_release_date(raw: str) -> date | None:
    """Aug 1, 2023 -> date(2023, 8, 1). Returns None on anything unexpected."""
    try:
        month_str, day_str, year_str = raw.replace(",", "").split()
        return date(int(year_str), MONTHS[month_str], int(day_str))
    except (ValueError, KeyError):
        return None


def top_tags(tags: dict[str, int], limit: int) -> list[str]:
    """Tag names, most-voted first."""
    ranked = sorted(tags.items(), key=lambda kv: kv[1], reverse=True)
    return [tag for tag, _votes in ranked[:limit]]


def build_embed_text(
    name: str, short_description: str, tags: dict[str, int]
) -> str | None:
    """The exact recipe from CLAUDE.md:

        {name}. {short_description} Tags: {top 15 tags by votes}.

    Returns None when there is nothing but a name to embed. Those 8,313
    records are Playtest and Closed Beta entries, not games.
    """
    if not short_description and not tags:
        return None

    parts = [f"{name}."]
    if short_description:
        parts.append(short_description)
    if tags:
        joined = ", ".join(top_tags(tags, TOP_TAGS_FOR_EMBEDDING))
        parts.append(f"Tags: {joined}.")
    return " ".join(parts)


def normalise_tags(raw: Any) -> dict[str, int]:
    """Source uses a dict when tags exist and an empty list when they do not."""
    return raw if isinstance(raw, dict) else {}


def parse_discount(raw: Any) -> int:
    """Percent off, clamped to 0-100.

    The source stores this as a string for 102,759 records and an int for
    36,205, so accept either. Anything unreadable means "not on sale", which
    leaves list_price_usd equal to price_usd — the safe direction to be wrong.
    """
    try:
        return max(0, min(100, int(float(raw))))
    except (TypeError, ValueError):
        return 0


def to_game_row(app_id: int, rec: dict[str, Any]) -> dict[str, Any]:
    price = rec.get("price") or 0.0
    tags = normalise_tags(rec.get("tags"))
    short_description = rec.get("short_description") or ""
    name = rec.get("name") or ""

    return {
        "app_id": app_id,
        "name": name,
        "short_description": short_description or None,
        "detailed_description": rec.get("detailed_description") or None,
        "release_date": parse_release_date(rec.get("release_date") or ""),
        # Price on the day of the scrape. list_price_usd is generated from this
        # and discount_pct, and is what search filters on.
        "price_usd": price,
        "discount_pct": parse_discount(rec.get("discount")),
        # Derived: the source has no is_free field.
        "is_free": price == 0,
        "windows": bool(rec.get("windows")),
        "mac": bool(rec.get("mac")),
        "linux": bool(rec.get("linux")),
        "positive_reviews": rec.get("positive") or 0,
        "negative_reviews": rec.get("negative") or 0,
        "metacritic_score": rec.get("metacritic_score") or None,
        "estimated_owners": rec.get("estimated_owners") or None,
        "header_image": rec.get("header_image") or None,
        "developers": rec.get("developers") or None,
        "publishers": rec.get("publishers") or None,
        "embed_text": build_embed_text(name, short_description, tags),
    }


def child_rows(
    app_id: int, rec: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Tag, genre and category rows for one game.

    dict.fromkeys deduplicates while preserving order: a duplicate genre in the
    source would otherwise violate the (app_id, genre) primary key mid-batch.
    """
    tag_rows = [
        {"app_id": app_id, "tag": tag, "votes": votes}
        for tag, votes in normalise_tags(rec.get("tags")).items()
    ]
    genre_rows = [
        {"app_id": app_id, "genre": genre}
        for genre in dict.fromkeys(rec.get("genres") or [])
    ]
    category_rows = [
        {"app_id": app_id, "category": category}
        for category in dict.fromkeys(rec.get("categories") or [])
    ]
    return tag_rows, genre_rows, category_rows


def batched(
    items: list[tuple[int, dict[str, Any]]], size: int
) -> Iterator[list[tuple[int, dict[str, Any]]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def upsert_batch(batch: list[tuple[int, dict[str, Any]]]) -> None:
    """One transaction per batch, so an interrupt loses at most this batch."""
    game_rows: list[dict[str, Any]] = []
    tag_rows: list[dict[str, Any]] = []
    genre_rows: list[dict[str, Any]] = []
    category_rows: list[dict[str, Any]] = []

    for app_id, rec in batch:
        game_rows.append(to_game_row(app_id, rec))
        tags, genres, categories = child_rows(app_id, rec)
        tag_rows.extend(tags)
        genre_rows.extend(genres)
        category_rows.extend(categories)

    with session_scope() as session:
        stmt = pg_insert(Game).values(game_rows)

        assignments: dict[str, Any] = {
            col.name: stmt.excluded[col.name]
            for col in Game.__table__.columns
            if col.name not in PRESERVE_ON_CONFLICT
        }

        # Keep the embedding unless the text it was built from changed. A
        # refreshed games.json can alter a description or its tags, and a vector
        # for text that no longer exists is worse than no vector at all: the
        # embed job skips non-NULL rows, so it would never be noticed. Nulling
        # it here puts the row straight back into the embed job's queue.
        # is_distinct_from rather than != so NULL on either side compares
        # correctly.
        table = Game.__table__
        text_changed = table.c.embed_text.is_distinct_from(stmt.excluded.embed_text)
        assignments["embedding"] = case(
            (text_changed, None), else_=table.c.embedding
        )
        assignments["embedding_model"] = case(
            (text_changed, None), else_=table.c.embedding_model
        )
        assignments["embedded_at"] = case(
            (text_changed, None), else_=table.c.embedded_at
        )

        session.execute(
            stmt.on_conflict_do_update(index_elements=["app_id"], set_=assignments)
        )

        if tag_rows:
            tag_stmt = pg_insert(GameTag).values(tag_rows)
            session.execute(
                tag_stmt.on_conflict_do_update(
                    index_elements=["app_id", "tag"],
                    set_={"votes": tag_stmt.excluded.votes},
                )
            )
        if genre_rows:
            genre_stmt = pg_insert(GameGenre).values(genre_rows)
            session.execute(genre_stmt.on_conflict_do_nothing())
        if category_rows:
            category_stmt = pg_insert(GameCategory).values(category_rows)
            session.execute(category_stmt.on_conflict_do_nothing())


def report() -> None:
    with session_scope() as session:
        for label, model in (
            ("games", Game),
            ("game_tags", GameTag),
            ("game_genres", GameGenre),
            ("game_categories", GameCategory),
        ):
            count = session.scalar(select(func.count()).select_from(model))
            print(f"  {label:<17} {count:>9,}")
        embeddable = session.scalar(
            select(func.count()).select_from(Game).where(Game.embed_text.isnot(None))
        )
        print(f"  {'with embed_text':<17} {embeddable:>9,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load games.json into Postgres.")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Re-process games already in the database instead of skipping them.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Load only the first N games."
    )
    args = parser.parse_args()

    print(f"reading {DATA_PATH} ...")
    with open(DATA_PATH, encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    print(f"  {len(data):,} records in file")

    with session_scope() as session:
        already = set(session.scalars(select(Game.app_id)).all())
    if already and not args.reload:
        print(f"  {len(already):,} already loaded, skipping (--reload to force)")

    items = [
        (int(app_id), rec)
        for app_id, rec in data.items()
        if args.reload or int(app_id) not in already
    ]
    if args.limit is not None:
        items = items[: args.limit]

    if not items:
        print("\nnothing to do.")
        report()
        return

    print(f"  {len(items):,} to process\n")
    for batch in tqdm(list(batched(items, BATCH_SIZE)), unit="batch", desc="loading"):
        upsert_batch(batch)

    print("\nrow counts:")
    report()


if __name__ == "__main__":
    main()
