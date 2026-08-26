"""Add discount_pct and a derived list_price_usd.

games.price_usd holds the price on the day of the Kaggle scrape, and that
scrape caught a Steam sale: 41,712 of 110,709 paid games (37.7%) were
discounted. Filtering "under $20" against it wrongly admits 3,004 games —
Rust reads as $19.99 when it actually costs $39.99.

The source carries a `discount` percentage that the first loader dropped. This
adds it, plus a generated column reversing it into the normal price.

list_price_usd is accurate to about a cent. Steam rounds sale prices down, so
$14.99 at -40% is stored as $8.99 and reverses to $14.98. Fine for filtering;
do not present it as an exact price.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-26

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Guarded at both ends: discount 0 means no sale, and 100 would divide by zero.
LIST_PRICE_EXPR = (
    "CASE WHEN discount_pct <= 0 OR discount_pct >= 100 THEN price_usd "
    "ELSE round(price_usd / (1 - discount_pct / 100.0), 2) END"
)


def upgrade() -> None:
    op.add_column(
        "games",
        sa.Column(
            "discount_pct", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "games",
        sa.Column(
            "list_price_usd",
            sa.Numeric(10, 2),
            sa.Computed(LIST_PRICE_EXPR, persisted=True),
            nullable=True,
        ),
    )
    op.create_index("ix_games_list_price_usd", "games", ["list_price_usd"])


def downgrade() -> None:
    op.drop_index("ix_games_list_price_usd", table_name="games")
    op.drop_column("games", "list_price_usd")
    op.drop_column("games", "discount_pct")
