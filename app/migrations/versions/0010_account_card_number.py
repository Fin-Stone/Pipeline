"""The card numbers an account has been known by.

A card account is keyed by its product — "Ocbc Rewards Card" — and never by its
number, because the number changes on reissue while the account continues, and
keying on it would fork one card's history in two.

That identity is right and it leaves a hole this table fills. A deposit
statement records paying the bill against the **number**: DBS writes
`Advice Bill Payment CCC - <sixteen digits>`, OCBC writes the number after
`BILL PAYMENT INB`. Nothing in the ledger connected those digits to the card
they settled, so the payment either went unmatched — money leaving the
household for nowhere — or was matched on amount alone, which cannot tell two
cards apart when both are paid in the same month.

A set that grows rather than a column that is overwritten, because the point is
that a card has been several numbers over its life and a payment to any of them
settled this account.

Only the last four are kept, masked. It is everything the matching needs, and a
full card number has no business in a household ledger.

Nothing is backfilled: the numbers live on the statements, so they arrive as
documents are imported or reparsed.

Revision ID: 0010_account_card_number
Revises: 0009_tenant_setting
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_account_card_number"
down_revision = "0009_tenant_setting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_card_number",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("card_number_masked", sa.String(32), nullable=False),
        sa.Column("first_seen", sa.Date(), nullable=False),
        sa.Column("last_seen", sa.Date(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "account_id", "card_number_masked", name="uq_account_card_number",
        ),
    )


def downgrade() -> None:
    op.drop_table("account_card_number")
