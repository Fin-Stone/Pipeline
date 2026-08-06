"""Identify a statement by its accounts and period, not by its bytes.

A bank issues one statement per account per period. That is the statement's
identity. The file carrying it is not: PDFs get re-downloaded, re-saved,
renamed and passed through tools that rewrite their metadata, and every one of
those produces different bytes for the same statement.

Keying only on bytes had a visible consequence. A statement uploaded twice as
two different files imported as two documents, and the second one's rows all
matched existing dedupe keys and were skipped — leaving a document with
balances attached and no transactions. In a household where two members both
diligently upload a shared account's statement, that is not an edge case; it is
the normal outcome.

`statement_key` is unique per tenant, so the second upload is recognised as the
same statement. A second upload whose *contents* differ is a different matter —
a reissued or corrected statement — and is quarantined for the operator to look
at rather than resolved silently in either direction.

Nullable, because a quarantined document was never parsed and so has no
accounts or period to key on. NULLs do not collide in a unique constraint,
which is the behaviour wanted here.

Revision ID: 0003_statement_key
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0003_statement_key"
down_revision = "0002_dummy_tenant"
branch_labels = None
depends_on = None


def _statement_key(doc_type, period_start, period_end, account_refs) -> str:
    """Mirrors app.domain.dedupe.statement_key.

    Duplicated deliberately: a migration must keep producing the same values
    years from now, and importing application code into it would let a later
    refactor silently change what an old migration does.
    """
    refs = "|".join(sorted(set(account_refs)))
    payload = "\x1f".join([
        doc_type or "",
        str(period_start or ""),
        str(period_end or ""),
        refs,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    with op.batch_alter_table("source_document") as batch:
        batch.add_column(sa.Column("statement_key", sa.String(64)))

    conn = op.get_bind()
    # Grouped in Python, not in SQL. `GROUP_CONCAT` is SQLite's spelling and
    # Postgres has `string_agg` with a different signature, so aggregating here
    # would make this migration run on one engine and fail on the other — which
    # it did, on the first real Postgres start. Joining refs into a string only
    # to split them again was also one comma in an account reference away from
    # producing a wrong key.
    #
    # Ordered so the result does not depend on the order an engine happens to
    # return rows in: where a ledger already holds two documents for one
    # statement, the lower id keeps the key on every engine.
    rows = conn.execute(sa.text("""
        SELECT d.id, d.tenant_id, d.doc_type, d.period_start, d.period_end,
               a.account_ref_masked AS ref
        FROM source_document d
        JOIN statement_balance b ON b.source_document_id = d.id
        JOIN account a ON a.id = b.account_id
        ORDER BY d.id
    """)).all()

    documents: dict[int, dict] = {}
    for row in rows:
        entry = documents.setdefault(row.id, {
            "tenant_id": row.tenant_id,
            "doc_type": row.doc_type,
            "period_start": row.period_start,
            "period_end": row.period_end,
            "refs": set(),
        })
        if row.ref:
            entry["refs"].add(row.ref)

    seen: set[tuple[int, str]] = set()
    for document_id, entry in documents.items():
        refs = entry["refs"]
        if not refs:
            continue
        key = _statement_key(
            entry["doc_type"], entry["period_start"], entry["period_end"], refs
        )
        tenant = entry["tenant_id"]
        # A ledger built before this migration can already contain two
        # documents for one statement — that is the situation it exists to
        # prevent. The first keeps the key; later ones are left null rather
        # than failing the migration, and show up as documents holding no
        # transactions.
        if (tenant, key) in seen:
            continue
        seen.add((tenant, key))
        conn.execute(
            sa.text("UPDATE source_document SET statement_key = :k WHERE id = :id"),
            {"k": key, "id": document_id},
        )

    with op.batch_alter_table("source_document") as batch:
        batch.create_unique_constraint(
            "uq_source_document_statement", ["tenant_id", "statement_key"]
        )


def downgrade() -> None:
    with op.batch_alter_table("source_document") as batch:
        batch.drop_constraint("uq_source_document_statement", type_="unique")
        batch.drop_column("statement_key")
