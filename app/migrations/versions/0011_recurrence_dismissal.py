"""Series the operator says are not a subscription.

Recurrence is a good guess and still a guess. Three payments of the same amount
a year apart are usually an insurance premium and are sometimes three people
settling up after three holidays — and the rules that find the real ones are
exactly the rules that occasionally find these. Widening the detector to catch
direct debits the bank never named made that trade explicitly: more real
commitments found, and the occasional wrong one.

The answer to that is not a stricter detector, which would go back to missing
seven insurance premiums. It is a cheap way to say no.

Keyed on what the operator was shown — a name and an amount — rather than on
transaction ids, which a reparse replaces. The pass is a pure function of the
ledger, re-derived on every request, so a dismissal has to survive the series
being computed again from nothing.

Revision ID: 0011_recurrence_dismissal
Revises: 0010_account_card_number
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_recurrence_dismissal"
down_revision = "0010_account_card_number"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recurrence_dismissal",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("merchant_norm", sa.Text(), nullable=False),
        sa.Column("amount_centre_minor", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "merchant_norm", "amount_centre_minor",
            name="uq_recurrence_dismissal",
        ),
    )


def downgrade() -> None:
    op.drop_table("recurrence_dismissal")
