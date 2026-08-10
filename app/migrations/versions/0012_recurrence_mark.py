"""Series the operator says *are* a subscription, on a period they name.

The mirror of 0011, and needed for the same reason from the other side. The
detector wants three occurrences and gaps that barely vary, and it is right to:
without those, "regular" is a claim the rows do not support. But a yearly
premium has two rows after two years, a quarterly bill lands whenever the
vendor gets round to invoicing, and a plan taken out last month has one — all
real commitments, none of them findable, and every one of them known to the
person paying it.

Loosening the detector to reach them is the wrong trade twice over: it is what
turned one monthly premium into three quarterly ones, and the operator's
knowledge is better evidence than any threshold would be anyway.

Keyed on the name and amount the operator was shown, exactly as a dismissal is,
because the pass is a pure function of the ledger and is re-derived on every
request — a mark has to survive the series being computed again from nothing,
and transaction ids do not survive a reparse.

`period_label` is stored because it is the part that cannot be inferred. That
is the whole point of the mark: the rows do not say how often this happens, and
the person does.

Revision ID: 0012_recurrence_mark
Revises: 0011_recurrence_dismissal
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_recurrence_mark"
down_revision = "0011_recurrence_dismissal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recurrence_mark",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("merchant_norm", sa.Text(), nullable=False),
        sa.Column("amount_centre_minor", sa.BigInteger(), nullable=False),
        sa.Column("period_label", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("marked_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "merchant_norm", "amount_centre_minor",
            name="uq_recurrence_mark",
        ),
    )


def downgrade() -> None:
    op.drop_table("recurrence_mark")
