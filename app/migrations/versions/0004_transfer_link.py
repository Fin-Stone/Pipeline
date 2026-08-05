"""Record which pairs of rows are one movement between our own accounts.

A transfer is recorded twice, once on each side, and both records are correct:
money did leave one account and arrive in another. What is wrong is counting
either as spending. Paying a credit card off is the plain case -- the payment
leaves a savings account and settles the card, while the purchases the card
made are already spending. Count the payment too and the month is overstated by
the size of the bill.

The claim lives beside the transactions rather than on them. Both rows are
facts about what a statement said and must not be edited; whether a pair is a
transfer is a judgement, and the rule for it will be revised as more of the
corpus is seen. Clearing this table and running the pass again is therefore a
supported operation, which it would not be if the claim were a column on `txn`.

A transaction belongs to at most one movement, enforced per tenant on each
side. Without that, a rebuild that ran twice would pair the same rows again and
quietly double the amount excluded from spending -- an error that makes the
total look better rather than worse, so nobody would go looking for it.

Revision ID: 0004_transfer_link
Revises: 0003_statement_key
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_transfer_link"
down_revision = "0003_statement_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transfer_link",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("out_txn_id", sa.Integer(), sa.ForeignKey("txn.id"), nullable=False),
        sa.Column("in_txn_id", sa.Integer(), sa.ForeignKey("txn.id"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("days_apart", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence", sa.String(64), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "out_txn_id", name="uq_transfer_link_out"),
        sa.UniqueConstraint("tenant_id", "in_txn_id", name="uq_transfer_link_in"),
    )


def downgrade() -> None:
    op.drop_table("transfer_link")
