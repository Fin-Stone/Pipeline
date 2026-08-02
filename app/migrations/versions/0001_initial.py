"""Initial ledger schema.

Architecture §1 in full, plus the Phase 1 additions. Phase 1 populates
source_document, account, txn and statement_balance; the enrichment and
recurrence tables are created now because the data model is the architecture —
separating immutable facts from mutable predictions from the start is what lets
history be re-classified later without touching the ledger underneath.

Deliberately portable: TEXT + CHECK rather than a Postgres ENUM, no
dialect-specific types or defaults. tests/test_migration.py asserts this
migration and app/storage/schema.py stay in agreement.

Revision ID: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

PARSE_STATUSES = ("imported", "imported_unverified", "quarantined")
ACCOUNT_KINDS = ("deposit", "card", "loan")
SOURCE_PROFILES = ("dummy", "prod")
ENRICHMENT_SOURCES = ("rule", "knn", "llm", "human")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ('" + "','".join(values) + "')"


def upgrade() -> None:
    op.create_table(
        "source_document",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("institution", sa.String(64), nullable=False),
        sa.Column("doc_type", sa.String(32), nullable=False),
        sa.Column("period_start", sa.Date()),
        sa.Column("period_end", sa.Date()),
        sa.Column("statement_date", sa.Date()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_method", sa.String(32), nullable=False, server_default="manual_upload"),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.String(64)),
        sa.Column("layout_fingerprint", sa.String(64)),
        sa.Column("parse_status", sa.String(32), nullable=False),
        sa.Column("source_profile", sa.String(16), nullable=False),
        sa.Column("source_relpath", sa.Text(), nullable=False),
        sa.CheckConstraint(_in_list("parse_status", PARSE_STATUSES), name="ck_source_document_parse_status"),
        sa.CheckConstraint(_in_list("source_profile", SOURCE_PROFILES), name="ck_source_document_source_profile"),
    )

    op.create_table(
        "account",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("institution", sa.String(64), nullable=False),
        # Masked account number for deposits; the card *product* for cards,
        # because card numbers change on reissue while the account continues.
        sa.Column("account_ref_masked", sa.String(64), nullable=False),
        sa.Column("sub_account_label", sa.String(128), nullable=False, server_default=""),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.UniqueConstraint(
            "institution", "account_ref_masked", "sub_account_label", "currency",
            name="uq_account_identity",
        ),
        sa.CheckConstraint(_in_list("kind", ACCOUNT_KINDS), name="ck_account_kind"),
    )

    op.create_table(
        "txn",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("source_document_id", sa.Integer(), sa.ForeignKey("source_document.id"), nullable=False),
        sa.Column("posted_date", sa.Date(), nullable=False),
        sa.Column("value_date", sa.Date()),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("fx_amount_minor", sa.BigInteger()),
        sa.Column("fx_currency", sa.String(3)),
        sa.Column("fx_rate", sa.Numeric(18, 8)),
        sa.Column("description_raw", sa.Text(), nullable=False),
        sa.Column("description_norm", sa.Text(), nullable=False),
        sa.Column("counterparty_norm", sa.Text(), nullable=False, server_default=""),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dedupe_key", sa.String(64), nullable=False, unique=True),
    )
    op.create_index("ix_txn_account_posted", "txn", ["account_id", "posted_date"])
    op.create_index("ix_txn_source_document", "txn", ["source_document_id"])

    op.create_table(
        "statement_balance",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("source_document_id", sa.Integer(), sa.ForeignKey("source_document.id"), nullable=False),
        sa.Column("opening_balance_minor", sa.BigInteger()),
        sa.Column("closing_balance_minor", sa.BigInteger()),
        sa.UniqueConstraint("account_id", "source_document_id", name="uq_statement_balance"),
    )

    op.create_table(
        "txn_enrichment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("txn_id", sa.Integer(), sa.ForeignKey("txn.id"), nullable=False),
        sa.Column("category", sa.String(64)),
        sa.Column("subcategory", sa.String(64)),
        sa.Column("beneficiary", sa.String(64)),
        sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("model_version", sa.String(64)),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_in_list("source", ENRICHMENT_SOURCES), name="ck_txn_enrichment_source"),
    )

    op.create_table(
        "recurrence_series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("merchant_norm", sa.Text(), nullable=False),
        sa.Column("amount_centre_minor", sa.BigInteger(), nullable=False),
        sa.Column("amount_tolerance_minor", sa.BigInteger(), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("period_label", sa.String(32)),
        sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("expected_next", sa.Date()),
        sa.Column("last_seen", sa.Date()),
        sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "txn_series_link",
        sa.Column("txn_id", sa.Integer(), sa.ForeignKey("txn.id"), primary_key=True),
        sa.Column("series_id", sa.Integer(), sa.ForeignKey("recurrence_series.id"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("txn_series_link")
    op.drop_table("recurrence_series")
    op.drop_table("txn_enrichment")
    op.drop_table("statement_balance")
    op.drop_index("ix_txn_source_document", table_name="txn")
    op.drop_index("ix_txn_account_posted", table_name="txn")
    op.drop_table("txn")
    op.drop_table("account")
    op.drop_table("source_document")
