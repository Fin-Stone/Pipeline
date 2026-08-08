"""Choices the operator made about how their ledger is read.

The transfer matcher's window is the first of these, and it is what forced the
table. How many days apart two legs of a transfer may be booked is not a
property of the machine — it is a property of the household's banks, and it
should survive a backup, a new box, and a change of engine exactly as their
category rules do. In `.env` it would be lost by the one operation whose whole
purpose is to preserve what a person decided.

Values are text, parsed by whoever reads them. A typed column per setting would
mean a migration for every new one, which is the tax this exists to avoid.

Revision ID: 0009_tenant_setting
Revises: 0008_payback_link
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_tenant_setting"
down_revision = "0008_payback_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_setting",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "key", name="uq_tenant_setting"),
    )
    # No rows seeded. An absent setting means "whatever the code defaults to",
    # which is a different and more useful state than a stored copy of today's
    # default — the latter would freeze every install at the value that
    # happened to be current when it was created, and a later improvement to
    # the default would reach nobody.


def downgrade() -> None:
    op.drop_table("tenant_setting")
