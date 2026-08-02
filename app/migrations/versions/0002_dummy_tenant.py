"""Move dummy documents into their own tenant.

Dummy and production documents shared a tenant, and therefore shared the two
uniqueness constraints that carry deduplication. That had a concrete
consequence: a real statement whose synthetic copy had already been imported
arrived with every one of its rows deduplicated away, leaving a document with
balances attached and no transactions. Four such documents existed in practice.

Production should not be able to observe dummy data at all — not in its
counts, not in its accounts, and above all not in its deduplication. A tenant
is precisely "a set of records that must never mix", so dummy documents move
into one of their own rather than a second isolation mechanism being invented.

Accounts are *split* rather than moved: the same savings account legitimately
has both dummy and production statements, so this creates a dummy-tenant copy
of any account that has dummy documents, repoints the dummy rows at it, and
leaves the production account untouched. An account that turns out to have had
only dummy documents is removed from the production tenant afterwards.

Revision ID: 0002_dummy_tenant
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0002_dummy_tenant"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    dummy_docs = conn.execute(sa.text(
        "SELECT DISTINCT tenant_id FROM source_document WHERE source_profile = 'dummy'"
    )).scalars().all()
    if not dummy_docs:
        return

    now = datetime.now(timezone.utc)
    for tenant_id in dummy_docs:
        slug = conn.execute(
            sa.text("SELECT slug FROM tenant WHERE id = :id"), {"id": tenant_id}
        ).scalar_one()
        dummy_slug = f"{slug}-dummy"

        dummy_tenant = conn.execute(
            sa.text("SELECT id FROM tenant WHERE slug = :slug"), {"slug": dummy_slug}
        ).scalar_one_or_none()
        if dummy_tenant is None:
            conn.execute(
                sa.text(
                    "INSERT INTO tenant (slug, name, status, created_at) "
                    "VALUES (:slug, :name, 'active', :now)"
                ),
                {"slug": dummy_slug, "name": f"{slug} (dummy)", "now": now},
            )
            dummy_tenant = conn.execute(
                sa.text("SELECT id FROM tenant WHERE slug = :slug"), {"slug": dummy_slug}
            ).scalar_one()

        # Every account that carries at least one dummy document gets a
        # dummy-tenant twin, and the dummy rows are repointed at it.
        accounts = conn.execute(sa.text("""
            SELECT DISTINCT t.account_id FROM txn t
            JOIN source_document d ON d.id = t.source_document_id
            WHERE d.tenant_id = :tid AND d.source_profile = 'dummy'
            UNION
            SELECT DISTINCT b.account_id FROM statement_balance b
            JOIN source_document d ON d.id = b.source_document_id
            WHERE d.tenant_id = :tid AND d.source_profile = 'dummy'
        """), {"tid": tenant_id}).scalars().all()

        for account_id in accounts:
            row = conn.execute(sa.text(
                "SELECT institution, account_ref_masked, sub_account_label, currency, kind "
                "FROM account WHERE id = :id"
            ), {"id": account_id}).one()

            twin = conn.execute(sa.text("""
                SELECT id FROM account WHERE tenant_id = :tid AND institution = :inst
                  AND account_ref_masked = :ref AND sub_account_label = :sub AND currency = :cur
            """), {"tid": dummy_tenant, "inst": row.institution, "ref": row.account_ref_masked,
                   "sub": row.sub_account_label, "cur": row.currency}).scalar_one_or_none()
            if twin is None:
                conn.execute(sa.text("""
                    INSERT INTO account (tenant_id, institution, account_ref_masked,
                                         sub_account_label, currency, kind)
                    VALUES (:tid, :inst, :ref, :sub, :cur, :kind)
                """), {"tid": dummy_tenant, "inst": row.institution, "ref": row.account_ref_masked,
                       "sub": row.sub_account_label, "cur": row.currency, "kind": row.kind})
                twin = conn.execute(sa.text("""
                    SELECT id FROM account WHERE tenant_id = :tid AND institution = :inst
                      AND account_ref_masked = :ref AND sub_account_label = :sub AND currency = :cur
                """), {"tid": dummy_tenant, "inst": row.institution, "ref": row.account_ref_masked,
                       "sub": row.sub_account_label, "cur": row.currency}).scalar_one()

            for table in ("txn", "statement_balance"):
                conn.execute(sa.text(f"""
                    UPDATE {table} SET tenant_id = :dtid, account_id = :twin
                    WHERE account_id = :old AND source_document_id IN (
                        SELECT id FROM source_document
                        WHERE tenant_id = :tid AND source_profile = 'dummy')
                """), {"dtid": dummy_tenant, "twin": twin, "old": account_id, "tid": tenant_id})

        conn.execute(sa.text(
            "UPDATE source_document SET tenant_id = :dtid "
            "WHERE tenant_id = :tid AND source_profile = 'dummy'"
        ), {"dtid": dummy_tenant, "tid": tenant_id})

        # Accounts that only ever held dummy documents no longer belong to the
        # production tenant.
        conn.execute(sa.text("""
            DELETE FROM account WHERE tenant_id = :tid
              AND id NOT IN (SELECT DISTINCT account_id FROM txn WHERE tenant_id = :tid)
              AND id NOT IN (SELECT DISTINCT account_id FROM statement_balance WHERE tenant_id = :tid)
        """), {"tid": tenant_id})


def downgrade() -> None:
    """Fold dummy tenants back into their parent.

    Deliberately does not restore the merged accounts: the split is the point
    of the upgrade, and re-merging would recreate the deduplication collision
    it exists to prevent. Dummy data is disposable — re-import it instead.
    """
    conn = op.get_bind()
    for dummy_id, slug in conn.execute(sa.text(
        "SELECT id, slug FROM tenant WHERE slug LIKE '%-dummy'"
    )).all():
        parent_slug = slug[: -len("-dummy")]
        parent = conn.execute(
            sa.text("SELECT id FROM tenant WHERE slug = :slug"), {"slug": parent_slug}
        ).scalar_one_or_none()
        if parent is None:
            continue
        for table in ("txn", "statement_balance", "source_document", "account"):
            conn.execute(
                sa.text(f"UPDATE {table} SET tenant_id = :p WHERE tenant_id = :d"),
                {"p": parent, "d": dummy_id},
            )
        conn.execute(sa.text("DELETE FROM tenant WHERE id = :d"), {"d": dummy_id})
