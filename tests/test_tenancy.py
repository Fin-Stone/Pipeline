"""Tenant isolation.

The system runs single-tenant today, so none of this is exercised in
production yet. It is tested now because the constraints it depends on are the
ones that are painful to add later, and because a leak between households'
financial records is the worst failure this system could have.

Each test runs against both engines via the `repository` fixture.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.domain.tenancy import MemberIdentity, TenantContext
from app.pipeline.ingest import ingest_inbox
from app.ports.repository import AccountRecord, BalanceRecord, DocumentRecord, TxnRecord
from app.storage import schema

from .fixtures.make_pdf import synthetic_statement, write_pdf
from .test_diagnostics import _registry_for


def _account(ref="01-1234567-8"):
    return AccountRecord(
        institution="Test Bank", account_ref_masked=ref,
        currency="SGD", kind="deposit", sub_account_label="Main",
    )


def _document(sha, **kwargs):
    return DocumentRecord(
        sha256=sha, institution="Test Bank", doc_type="acc",
        period_start=date(2025, 1, 1), period_end=date(2025, 1, 31),
        storage_path="/dev/null", parse_status="imported",
        source_profile="dummy", source_relpath="a.pdf",
        parser_version="test@1", **kwargs,
    )


def _txn(key, account=None):
    return TxnRecord(
        account_key=account or _account(),
        posted_date=date(2025, 1, 3), amount_minor=-1000, currency="SGD",
        description_raw="Coffee", description_norm="COFFEE", counterparty_norm="COFFEE",
        dedupe_key=key, seq=0,
    )


class TestTenantResolution:
    def test_default_tenant_and_member_exist(self, repository, context):
        assert context.tenant_id is not None
        assert context.member_id is not None
        assert context.role == "owner"

    def test_resolution_is_idempotent(self, repository):
        first = repository.resolve_context("household", "a@example.com")
        second = repository.resolve_context("household", "a@example.com")
        assert (first.tenant_id, first.member_id) == (second.tenant_id, second.member_id)

    def test_separate_tenants_get_separate_ids(self, repository, context, other_context):
        assert context.tenant_id != other_context.tenant_id

    def test_several_members_share_one_tenant(self, repository):
        """A family plan: one tenant, several members with their own logins."""
        tenant_id = repository.ensure_tenant("family")
        parent = repository.ensure_member(tenant_id, display_name="Parent", email="p@example.com", role="owner")
        child = repository.ensure_member(tenant_id, display_name="Child", email="c@example.com", role="child")
        assert parent != child

        with repository.engine.connect() as conn:
            count = conn.execute(
                select(func.count()).select_from(schema.member)
                .where(schema.member.c.tenant_id == tenant_id)
            ).scalar_one()
        assert count == 2


class TestSsoIdentity:
    IDENTITY = MemberIdentity(
        issuer="https://accounts.example.com", subject="sub-abc-123",
        email="a@example.com", display_name="A",
    )

    def test_identity_resolves_to_its_member(self, repository):
        tenant_id = repository.ensure_tenant("household")
        member_id = repository.ensure_member(
            tenant_id, display_name="A", email="a@example.com", identity=self.IDENTITY
        )
        resolved = repository.find_member_by_identity(self.IDENTITY)
        assert resolved is not None
        assert (resolved.tenant_id, resolved.member_id) == (tenant_id, member_id)

    def test_unknown_identity_resolves_to_nothing(self, repository):
        assert repository.find_member_by_identity(
            MemberIdentity(issuer="https://other", subject="nobody")
        ) is None

    def test_first_login_attaches_to_an_invited_member(self, repository):
        """Someone invited by email, then logging in via SSO, must become the
        same member — not a second one holding half their history."""
        tenant_id = repository.ensure_tenant("household")
        invited = repository.ensure_member(tenant_id, display_name="A", email="a@example.com", role="adult")
        after_login = repository.ensure_member(
            tenant_id, display_name="A", email="a@example.com", identity=self.IDENTITY
        )
        assert invited == after_login

    def test_the_same_subject_is_one_member(self, repository):
        tenant_id = repository.ensure_tenant("household")
        first = repository.ensure_member(tenant_id, display_name="A", identity=self.IDENTITY)
        second = repository.ensure_member(tenant_id, display_name="A renamed", identity=self.IDENTITY)
        assert first == second


class TestIsolation:
    def test_the_same_statement_imports_for_two_tenants(self, repository, context, other_context):
        """Two households can hold the same file. Global sha256 uniqueness
        would silently treat the second as a duplicate."""
        sha = "a" * 64
        first = repository.insert_document(context, _document(sha), [], [_txn("k" * 64)])
        second = repository.insert_document(other_context, _document(sha), [], [_txn("k" * 64)])

        assert first.document_id != second.document_id
        assert first.txns_inserted == 1 and second.txns_inserted == 1

    def test_identical_transactions_do_not_collide_across_tenants(self, repository, context, other_context):
        repository.insert_document(context, _document("a" * 64), [], [_txn("k" * 64)])
        repository.insert_document(other_context, _document("b" * 64), [], [_txn("k" * 64)])
        assert repository.counts(context).txns == 1
        assert repository.counts(other_context).txns == 1

    def test_the_same_account_reference_is_two_accounts(self, repository, context, other_context):
        """Two households banking at the same institution must not merge."""
        balances = [BalanceRecord(account_key=_account(), opening_balance_minor=0, closing_balance_minor=0)]
        repository.insert_document(context, _document("a" * 64), balances, [])
        repository.insert_document(other_context, _document("b" * 64), balances, [])

        with repository.engine.connect() as conn:
            rows = conn.execute(
                select(schema.account.c.tenant_id).where(
                    schema.account.c.account_ref_masked == "01-1234567-8"
                )
            ).scalars().all()
        assert sorted(rows) == sorted([context.tenant_id, other_context.tenant_id])

    def test_statement_balances_are_tenant_scoped(self, repository, context, other_context):
        balances = [BalanceRecord(account_key=_account(), opening_balance_minor=1000, closing_balance_minor=2000)]
        repository.insert_document(context, _document("a" * 64), balances, [])
        repository.insert_document(other_context, _document("b" * 64), balances, [])

        with repository.engine.connect() as conn:
            rows = conn.execute(
                select(schema.statement_balance.c.tenant_id).order_by(schema.statement_balance.c.tenant_id)
            ).scalars().all()
        assert rows == [context.tenant_id, other_context.tenant_id]

    def test_reads_never_cross_tenants(self, repository, context, other_context):
        repository.insert_document(context, _document("a" * 64), [], [_txn("k" * 64)])

        assert repository.get_document_id(context, "a" * 64) is not None
        assert repository.get_document_id(other_context, "a" * 64) is None
        assert repository.existing_dedupe_keys(context, ["k" * 64]) == {"k" * 64}
        assert repository.existing_dedupe_keys(other_context, ["k" * 64]) == set()

    def test_counts_are_per_tenant(self, repository, context, other_context):
        repository.insert_document(context, _document("a" * 64), [], [_txn("k" * 64)])
        assert repository.counts(context).documents == 1
        assert repository.counts(other_context).documents == 0

    def test_idempotency_still_holds_within_a_tenant(self, repository, context):
        repository.insert_document(context, _document("a" * 64), [], [_txn("k" * 64)])
        again = repository.insert_document(context, _document("a" * 64), [], [_txn("k" * 64)])
        assert again.txns_inserted == 0
        assert repository.counts(context).txns == 1


class TestPipelineRunsUnderATenant:
    def test_ingest_attributes_documents_to_the_context(
        self, config, repository, context, blob_store, notifier
    ):
        path = write_pdf(
            config.inbox_dir / "dummy" / "a.pdf",
            synthetic_statement([("03 Jun", "Salary", "+2,000.00")],
                                opening="1,000.00", closing="3,000.00"),
        )
        ingest_inbox(config, context, repository, blob_store, notifier, _registry_for(path))

        with repository.engine.connect() as conn:
            row = conn.execute(
                select(schema.source_document.c.tenant_id, schema.source_document.c.uploaded_by_member_id)
            ).one()
        assert row.tenant_id == context.tenant_id
        assert row.uploaded_by_member_id == context.member_id

    def test_two_tenants_ingesting_the_same_file_both_succeed(
        self, config, repository, context, other_context, blob_store, notifier
    ):
        path = write_pdf(
            config.inbox_dir / "dummy" / "a.pdf",
            synthetic_statement([("03 Jun", "Salary", "+2,000.00")],
                                opening="1,000.00", closing="3,000.00"),
        )
        registry = _registry_for(path)
        first = ingest_inbox(config, context, repository, blob_store, notifier, registry)
        second = ingest_inbox(config, other_context, repository, blob_store, notifier, registry)

        assert first.imported == 1 and second.imported == 1
        assert repository.counts(context).txns == 1
        assert repository.counts(other_context).txns == 1


class TestRoles:
    @pytest.mark.parametrize("role,expected", [
        ("owner", True), ("adult", True), ("child", False), ("viewer", False),
    ])
    def test_read_only_roles_are_identified(self, role, expected):
        assert TenantContext(tenant_id=1, member_id=1, role=role).can_write is expected
