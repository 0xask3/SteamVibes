"""SQLAlchemy 2.0 models.

These mirror alembic/versions/0001_initial.py. If you change anything here you
owe the database a migration — the models do not create tables.
"""

from datetime import date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Must match the vector() dimension in the migration. nomic-embed-text emits
# 768. Weekend 3's bge-m3 emits 1024 — changing this is a schema change, so it
# travels with a migration, never alone.
EMBEDDING_DIM = 768


class Base(DeclarativeBase):
    pass


class Game(Base):
    __tablename__ = "games"

    # Steam's identifier, not ours — never generated.
    app_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    short_description: Mapped[str | None] = mapped_column(Text)
    detailed_description: Mapped[str | None] = mapped_column(Text)

    release_date: Mapped[date | None] = mapped_column(Date)

    # Kaggle scrapes Steam's US storefront. price_eur arrives in Weekend 3 from
    # the storefront API with ?cc=de — do not treat this as euros.
    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    # Derived at ingest as (price == 0). The source has no such field, and some
    # price-0 rows are unreleased rather than genuinely free.
    is_free: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    windows: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    mac: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    linux: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    positive_reviews: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    negative_reviews: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # Maintained by Postgres, so the search filter reads `total_reviews > :n`
    # instead of recomputing a sum on every row of every query.
    total_reviews: Mapped[int] = mapped_column(
        Integer,
        Computed("positive_reviews + negative_reviews", persisted=True),
    )

    metacritic_score: Mapped[int | None] = mapped_column(Integer)
    estimated_owners: Mapped[str | None] = mapped_column(Text)
    header_image: Mapped[str | None] = mapped_column(Text)

    # Displayed, never filtered on — an array beats a join table here.
    developers: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    publishers: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    # The exact string handed to the embedding model. Stored so a bad result
    # can be traced to what was actually embedded, and so Weekend 3 re-embeds
    # identical text rather than rebuilding it and drifting.
    embed_text: Mapped[str | None] = mapped_column(Text)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))

    # NULL embedding means "not embedded yet" — that is what makes the embed
    # job resumable. These two say which model produced the vector and when,
    # so stale rows are identifiable after a model swap.
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tags: Mapped[list["GameTag"]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )
    genres: Mapped[list["GameGenre"]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )
    categories: Mapped[list["GameCategory"]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_games_price_usd", "price_usd"),
        Index("ix_games_release_date", "release_date"),
        Index("ix_games_total_reviews", "total_reviews"),
        # Partial index: the embed job asks "what is left?" repeatedly, and this
        # keeps that question cheap even as the unembedded set shrinks to zero.
        Index(
            "ix_games_unembedded",
            "app_id",
            postgresql_where=text("embedding IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Game {self.app_id} {self.name!r}>"


class GameTag(Base):
    """One row per (game, tag). Votes are why this is a table and not an array."""

    __tablename__ = "game_tags"

    app_id: Mapped[int] = mapped_column(
        ForeignKey("games.app_id", ondelete="CASCADE"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(Text, primary_key=True)
    votes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    game: Mapped[Game] = relationship(back_populates="tags")

    __table_args__ = (
        Index("ix_game_tags_tag", "tag"),
        # (app_id, votes) serves "top 15 tags by votes for this game" — Postgres
        # scans a btree backwards as happily as forwards, so no DESC needed.
        Index("ix_game_tags_app_votes", "app_id", "votes"),
    )

    def __repr__(self) -> str:
        return f"<GameTag {self.app_id} {self.tag!r} {self.votes}>"


class GameGenre(Base):
    __tablename__ = "game_genres"

    app_id: Mapped[int] = mapped_column(
        ForeignKey("games.app_id", ondelete="CASCADE"), primary_key=True
    )
    genre: Mapped[str] = mapped_column(Text, primary_key=True)

    game: Mapped[Game] = relationship(back_populates="genres")

    __table_args__ = (Index("ix_game_genres_genre", "genre"),)

    def __repr__(self) -> str:
        return f"<GameGenre {self.app_id} {self.genre!r}>"


class GameCategory(Base):
    """Steam's own categories — 'Multi-player', 'Co-op', 'Steam Achievements'.

    Weekend 2's `multiplayer` filter reads these; they are more reliable for
    that than community tags.
    """

    __tablename__ = "game_categories"

    app_id: Mapped[int] = mapped_column(
        ForeignKey("games.app_id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[str] = mapped_column(Text, primary_key=True)

    game: Mapped[Game] = relationship(back_populates="categories")

    __table_args__ = (Index("ix_game_categories_category", "category"),)

    def __repr__(self) -> str:
        return f"<GameCategory {self.app_id} {self.category!r}>"
