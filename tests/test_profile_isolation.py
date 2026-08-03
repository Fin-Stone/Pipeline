"""Dummy documents must not exist as far as production is concerned.

Not in its counts, not in its accounts, and — the one that actually caused
damage — not in its deduplication. Sharing a tenant meant a real statement
whose synthetic copy had already been imported arrived with every row
deduplicated away, leaving a document with balances and no transactions.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.config import PROFILE_DUMMY, PROFILE_PROD, ConfigError
from app.pipeline.ingest import ingest_inbox
from app.pipeline.quarantine import REASON_SUFFIX, export_originals
from app.pipeline.stage import stage
from app.storage import schema

from .fixtures.make_pdf import synthetic_signature, synthetic_statement, write_pdf
from .test_pipeline import SyntheticAdapter

from app.parsers.registry import AdapterRegistry


ROWS = [("03 Jun", "Salary", "+2,000.00")]


def _registry():
    registry = AdapterRegistry()
    registry.register(SyntheticAdapter(), synthetic_signature())
    return registry


def _contexts(repository, config):
    return (
        repository.resolve_context(config.tenant_for(PROFILE_DUMMY), config.member_email),
        repository.resolve_context(config.tenant_for(PROFILE_PROD), config.member_email),
    )


def _place(config, profile, name="statement.pdf"):
    return write_pdf(
        config.uploads_for(profile) / name,
        synthetic_statement(ROWS, opening="1,000.00", closing="3,000.00"),
    )


@pytest.fixture
def prod_config(config):
    """The same config with the prod gate opened, as an operator would."""
    from dataclasses import replace
    return replace(config, allow_prod=True)


class TestTenantMapping:
    def test_profiles_map_to_different_tenants(self, config):
        assert config.tenant_for(PROFILE_PROD) != config.tenant_for(PROFILE_DUMMY)

    def test_prod_keeps_the_plain_tenant_slug(self, config):
        """Production is the real ledger; it should not be the one wearing a
        suffix."""
        assert config.tenant_for(PROFILE_PROD) == config.tenant_slug

    def test_unknown_profile_is_rejected(self, config):
        with pytest.raises(ConfigError):
            config.tenant_for("staging")


class TestIsolation:
    def test_the_same_statement_imports_under_both_profiles(
        self, prod_config, repository, blob_store, notifier
    ):
        """The regression this exists for: a dummy copy of a real statement
        must not consume the real one's transactions."""
        dummy_ctx, prod_ctx = _contexts(repository, prod_config)
        registry = _registry()

        _place(prod_config, PROFILE_DUMMY)
        stage(prod_config, PROFILE_DUMMY, repository=repository, context=dummy_ctx)
        dummy_run = ingest_inbox(prod_config, dummy_ctx, repository, blob_store, notifier,
                                 registry, profile=PROFILE_DUMMY)

        _place(prod_config, PROFILE_PROD)
        stage(prod_config, PROFILE_PROD, repository=repository, context=prod_ctx)
        prod_run = ingest_inbox(prod_config, prod_ctx, repository, blob_store, notifier,
                                registry, profile=PROFILE_PROD)

        assert dummy_run.imported == 1 and dummy_run.txns_inserted == 1
        # Byte-identical file, same transactions — and it must still land.
        assert prod_run.imported == 1 and prod_run.txns_inserted == 1

    def test_a_prod_document_never_has_its_rows_deduplicated_away(
        self, prod_config, repository, blob_store, notifier
    ):
        """The symptom that surfaced: balances recorded, zero transactions."""
        dummy_ctx, prod_ctx = _contexts(repository, prod_config)
        registry = _registry()

        for profile, ctx in ((PROFILE_DUMMY, dummy_ctx), (PROFILE_PROD, prod_ctx)):
            _place(prod_config, profile)
            stage(prod_config, profile, repository=repository, context=ctx)
            ingest_inbox(prod_config, ctx, repository, blob_store, notifier, registry, profile=profile)

        with repository.engine.connect() as conn:
            documents = conn.execute(
                select(schema.source_document.c.id)
                .where(schema.source_document.c.tenant_id == prod_ctx.tenant_id)
            ).scalars().all()
            for document_id in documents:
                rows = conn.execute(
                    select(func.count()).select_from(schema.txn)
                    .where(schema.txn.c.source_document_id == document_id)
                ).scalar_one()
                assert rows > 0, "a prod document imported with no transactions"

    def test_counts_do_not_see_across_profiles(
        self, prod_config, repository, blob_store, notifier
    ):
        dummy_ctx, prod_ctx = _contexts(repository, prod_config)
        registry = _registry()

        _place(prod_config, PROFILE_DUMMY)
        stage(prod_config, PROFILE_DUMMY, repository=repository, context=dummy_ctx)
        ingest_inbox(prod_config, dummy_ctx, repository, blob_store, notifier,
                     registry, profile=PROFILE_DUMMY)

        assert repository.counts(dummy_ctx).documents == 1
        assert repository.counts(prod_ctx).documents == 0
        assert repository.counts(prod_ctx).txns == 0

    def test_accounts_do_not_leak_across_profiles(
        self, prod_config, repository, blob_store, notifier
    ):
        """A dummy statement must not create an account production can see."""
        dummy_ctx, prod_ctx = _contexts(repository, prod_config)
        registry = _registry()

        _place(prod_config, PROFILE_DUMMY)
        stage(prod_config, PROFILE_DUMMY, repository=repository, context=dummy_ctx)
        ingest_inbox(prod_config, dummy_ctx, repository, blob_store, notifier,
                     registry, profile=PROFILE_DUMMY)

        assert repository.counts(dummy_ctx).accounts == 1
        assert repository.counts(prod_ctx).accounts == 0


class TestQuarantineIsolation:
    """Separating the ledger is worth nothing if the failure detail beside it is
    shared. A reason file carries the document's real filename, the rows parsed
    out of it, its balances and its account references — the same data the
    tenant split exists to keep apart.
    """

    def _fail_under(self, prod_config, repository, blob_store, notifier, profile, ctx):
        _place(prod_config, profile)
        stage(prod_config, profile, repository=repository, context=ctx)
        # An empty registry claims nothing, so the document quarantines.
        return ingest_inbox(prod_config, ctx, repository, blob_store, notifier,
                            AdapterRegistry(), profile=profile)

    def test_one_failure_per_tenant_directory_and_none_at_the_root(
        self, prod_config, repository, blob_store, notifier
    ):
        """The documents here are byte-identical, so they share a digest — and a
        reason file is named by digest. Flat, the second overwrote the first and
        production's failure silently became the dummy one."""
        dummy_ctx, prod_ctx = _contexts(repository, prod_config)
        for profile, ctx in ((PROFILE_DUMMY, dummy_ctx), (PROFILE_PROD, prod_ctx)):
            summary = self._fail_under(prod_config, repository, blob_store, notifier, profile, ctx)
            assert summary.quarantined == 1

        dummy_reasons = list(prod_config.quarantine_dir_for(PROFILE_DUMMY).glob(f"*{REASON_SUFFIX}"))
        prod_reasons = list(prod_config.quarantine_dir_for(PROFILE_PROD).glob(f"*{REASON_SUFFIX}"))

        assert len(dummy_reasons) == 1 and len(prod_reasons) == 1
        assert dummy_reasons[0].name == prod_reasons[0].name, "same bytes, same digest"
        assert dummy_reasons[0].parent != prod_reasons[0].parent
        # Nothing may sit in the shared root, which is what the split replaced.
        assert not list(prod_config.quarantine_dir.glob(f"*{REASON_SUFFIX}"))

    def test_a_reason_file_records_which_profile_it_came_from(
        self, prod_config, repository, blob_store, notifier
    ):
        """So a reason says whose it is without relying on where it sits."""
        _, prod_ctx = _contexts(repository, prod_config)
        self._fail_under(prod_config, repository, blob_store, notifier, PROFILE_PROD, prod_ctx)

        reason = next(prod_config.quarantine_dir_for(PROFILE_PROD).glob(f"*{REASON_SUFFIX}"))
        assert json.loads(reason.read_text(encoding="utf-8"))["source_profile"] == PROFILE_PROD

    def test_exported_originals_do_not_mix(
        self, prod_config, repository, blob_store, notifier
    ):
        """The export is where a real statement becomes a recognisable file, so
        it is the place a leak would actually be readable."""
        dummy_ctx, prod_ctx = _contexts(repository, prod_config)
        for profile, ctx in ((PROFILE_DUMMY, dummy_ctx), (PROFILE_PROD, prod_ctx)):
            self._fail_under(prod_config, repository, blob_store, notifier, profile, ctx)

        for profile in (PROFILE_DUMMY, PROFILE_PROD):
            written = export_originals(
                prod_config.quarantine_dir_for(profile),
                prod_config.quarantine_files_dir_for(profile),
                blob_store,
            )
            assert len(written) == 1

        assert (prod_config.quarantine_files_dir_for(PROFILE_DUMMY)
                != prod_config.quarantine_files_dir_for(PROFILE_PROD))
