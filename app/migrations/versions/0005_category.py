"""Give the taxonomy to the tenant instead of to the schema.

Categories as rows, seeded per tenant and editable, rather than an enum. A
household's budgeting structure is theirs, and a set fixed in code makes every
disagreement with it a migration.

Two consequences the shape has to carry. Rules reference a category by **id**,
so renaming one cannot orphan the rules pointing at it. And
`txn_enrichment.category` keeps the resolved *name* as a snapshot of what was
decided, which is what makes reads cheap -- so renaming or merging has to
rewrite those rows, an operation that must exist rather than being left to
hand-editing.

Nothing is seeded here. A tenant may exist before it has an opinion about
categories, and inventing one during a migration would put fourteen rows in
front of an operator who never asked for them. Seeding happens on first use,
where it can be declined.

Revision ID: 0005_category
Revises: 0004_transfer_link
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_category"
down_revision = "0004_transfer_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "category",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_category_name"),
    )
    op.create_table(
        "category_rule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("category.id"), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "pattern", "category_id", name="uq_category_rule"),
    )


def downgrade() -> None:
    op.drop_table("category_rule")
    op.drop_table("category")
