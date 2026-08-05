"""Let the operator say "this is a transfer" when the matcher cannot.

The automatic pass pairs only what it can prove: equal magnitude, opposite
signs, within a few days, across two accounts. Three real cases fall outside
that and all of them are ordinary.

- The counterpart is at a bank this ledger does not hold. There is no second
  row to pair with, and there never will be.
- The amounts differ — a partial payment, or a fee taken in between.
- Two candidates fit equally well, so the matcher refused rather than guessed.

`in_txn_id` therefore becomes nullable. A link with one side is the operator
saying they know where the money went even though nothing here can corroborate
it, which is a weaker claim than a matched pair and is recorded as a different
shape rather than dressed up as the same one.

SQLite cannot ALTER a column's nullability, so the table is rebuilt. It holds
derived state that one command regenerates, but the rebuild copies rather than
truncates: an operator's manual pairings are not regenerable and must survive.

Revision ID: 0007_manual_transfer
Revises: 0006_hidden_txn
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_manual_transfer"
down_revision = "0006_hidden_txn"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transfer_link") as batch:
        batch.alter_column("in_txn_id", existing_type=sa.Integer(), nullable=True)
        # How the link came to exist. 'auto' is the matcher; 'manual' is the
        # operator, and only the second may be one-sided.
        batch.add_column(
            sa.Column("origin", sa.String(16), nullable=False, server_default="auto")
        )


def downgrade() -> None:
    with op.batch_alter_table("transfer_link") as batch:
        batch.drop_column("origin")
        batch.alter_column("in_txn_id", existing_type=sa.Integer(), nullable=False)
