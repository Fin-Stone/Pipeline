"""Let the operator take a transaction out of the picture.

Not a correction and not a transfer: both of those say what a row *is*. This
says what should count. A one-off house deposit is real spending and still not
representative of anything, and leaving it in drags a monthly average for a
year.

Held beside the ledger like every other judgement, so the transaction is
untouched and the decision is reversible. What is hidden stays countable: the
total of hidden rows is reported wherever hiding is offered, because a figure
that quietly omits things is worth less than one that says what it omitted.

Revision ID: 0006_hidden_txn
Revises: 0005_category
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_hidden_txn"
down_revision = "0005_category"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hidden_txn",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("txn_id", sa.Integer(), sa.ForeignKey("txn.id"), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "txn_id", name="uq_hidden_txn"),
    )


def downgrade() -> None:
    op.drop_table("hidden_txn")
