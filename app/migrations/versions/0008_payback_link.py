"""Money that came back for one specific charge.

One person pays for a group and the others settle up afterwards. The ledger
records that faithfully and reads it wrongly: $300 sits in Dining and $270
arrives as income from nowhere, when the household spent $30 and earned nothing.

`transfer_link` cannot express it. A transfer is money moving between the
household's own accounts, so neither leg is real activity and both are excluded
— which here would erase the household's own $30 share along with everyone
else's. A payback is a different claim: the inflow is not income, and the charge
was real but smaller than it looks.

So this reduces the charge rather than removing it. The signs already do the
arithmetic: the expense is negative and the payback positive.

An inflow settles one charge and no other, enforced by the unique constraint. A
payback consumed by two charges would discount both with one person's money.

Revision ID: 0008_payback_link
Revises: 0007_manual_transfer
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_payback_link"
down_revision = "0007_manual_transfer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payback_link",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("expense_txn_id", sa.Integer(), sa.ForeignKey("txn.id"), nullable=False),
        sa.Column("income_txn_id", sa.Integer(), sa.ForeignKey("txn.id"), nullable=False),
        # Stored rather than derived, so splitting one transfer across two
        # charges becomes possible later without a migration.
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "income_txn_id", name="uq_payback_income"),
    )
    op.create_index("ix_payback_expense", "payback_link", ["tenant_id", "expense_txn_id"])


def downgrade() -> None:
    op.drop_index("ix_payback_expense", table_name="payback_link")
    op.drop_table("payback_link")
