"""End-to-end pipeline behaviour, run against every supported engine.

Everything here goes through the same `repository` fixture, which is
parametrized over SQLite and (when TEST_DATABASE_URL is set) Postgres.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.domain.models import DEPOSIT, DOC_TYPE_ACCOUNT, ParsedAccount, ParsedDocument, ParsedTxn
from app.parsers import fingerprint as fingerprinting
from app.parsers import pdfio
from app.parsers.registry import AdapterRegistry, AmbiguousLayout, UnknownLayout
from app.pipeline.ingest import ingest_inbox, reparse
from app.pipeline.quarantine import REASON_SUFFIX
from app.pipeline.stage import stage
from app.pipeline.validate import validate
from app.ports.parser import ParseError
from app.ports.repository import STATUS_IMPORTED, STATUS_IMPORTED_UNVERIFIED

from .fixtures.make_pdf import synthetic_signature, synthetic_statement, write_pdf


def _statement_pdf(path: Path, rows, *, opening="1,000.00", closing="1,000.00", **kwargs) -> Path:
    return write_pdf(path, synthetic_statement(rows, opening=opening, closing=closing, **kwargs))


class SyntheticAdapter:
    """Adapter for the synthetic fixture layout, reusing the Trust table logic."""

    name = "test.synthetic"
    version = "1.0.0"
    institution = "Test Bank Placeholder"
    doc_type = DOC_TYPE_ACCOUNT

    def parse(self, path: Path, *, password: str | None = None) -> ParsedDocument:
        from app.parsers.trust import base

        document = pdfio.load(Path(path), password=password)
        period_start, period_end = base.find_period(document, "Statement period")
        bands = base.header_bands(base.find_header(document))
        lines = [l for l in base.transaction_lines(document) if not base.is_skippable(l)]

        opening = closing = None
        txns = []
        for row in base.assemble_rows(lines, bands):
            if row.label == "previous balance":
                opening = base.balance_amount(row.sgd_text, owed_is_negative=False)
            elif row.label == "closing balance":
                closing = base.balance_amount(row.sgd_text, owed_is_negative=False)
            else:
                txns.append(base.build_txn(row, period_start, period_end))

        return ParsedDocument(
            institution=self.institution,
            doc_type=self.doc_type,
            period_start=period_start,
            period_end=period_end,
            parser_version=f"{self.name}@{self.version}",
            accounts=(ParsedAccount(
                account_ref_masked="01-1234567-8",
                sub_account_label="Main Account",
                currency="SGD",
                kind=DEPOSIT,
                txns=tuple(txns),
                opening_balance_minor=opening,
                closing_balance_minor=closing,
            ),),
        )


@pytest.fixture
def registry_for(config):
    def build(path: Path | None = None) -> AdapterRegistry:
        registry = AdapterRegistry()
        registry.register(SyntheticAdapter(), synthetic_signature())
        return registry
    return build


def _run(config, context, repository, blob_store, notifier, registry):
    return ingest_inbox(config, context, repository, blob_store, notifier, registry)


class TestStaging:
    def test_discovers_nested_folders_recursively(self, config, repository, context):
        for relpath in ["BankA/acc/jan.pdf", "BankA/cc/feb.pdf", "BankB/2024/mar.pdf"]:
            _statement_pdf(config.uploads_for("dummy") / relpath, [])
        result = stage(config, "dummy", repository=repository, context=context)
        assert result.discovered == 3
        assert result.staged == 3
        assert (config.inbox_dir / "dummy" / "BankB" / "2024" / "mar.pdf").exists()

    def test_preserves_relative_layout(self, config, repository, context):
        _statement_pdf(config.uploads_for("dummy") / "BankA" / "acc" / "jan.pdf", [])
        stage(config, "dummy", repository=repository, context=context)
        assert (config.inbox_dir / "dummy" / "BankA" / "acc" / "jan.pdf").exists()

    def test_is_idempotent(self, config, repository, context):
        _statement_pdf(config.uploads_for("dummy") / "a.pdf", [])
        assert stage(config, "dummy", repository=repository, context=context).staged == 1
        second = stage(config, "dummy", repository=repository, context=context)
        assert second.staged == 0 and second.already_staged == 1

    def test_ignores_non_documents(self, config, repository, context):
        root = config.uploads_for("dummy")
        root.mkdir(parents=True, exist_ok=True)
        (root / "notes.txt").write_text("not a statement")
        assert stage(config, "dummy", repository=repository, context=context).discovered == 0

    def test_refuses_prod_without_explicit_opt_in(self, config, repository, context):
        """uploads/prod holds real financial data; processing it must be a
        deliberate act. See docs/development-rules.md Rule 2."""
        from app.config import ConfigError
        with pytest.raises(ConfigError, match="FINSTONE_ALLOW_PROD"):
            stage(config, "prod", repository=repository, context=context)


class TestIngest:
    def test_imports_and_reconciles(self, config, repository, context, blob_store, notifier, registry_for):
        path = _statement_pdf(
            config.inbox_dir / "dummy" / "a.pdf",
            [("03 Jun", "Salary", "+2,000.00"), ("10 Jun", "Rent", "1,500.00")],
            opening="1,000.00", closing="1,500.00",
        )
        summary = _run(config, context, repository, blob_store, notifier, registry_for(path))
        assert summary.imported == 1 and summary.quarantined == 0
        counts = repository.counts(context)
        assert counts.documents == 1 and counts.txns == 2 and counts.accounts == 1

    def test_is_idempotent_across_runs(self, config, repository, context, blob_store, notifier, registry_for):
        """Staging and ingesting the same document twice must add nothing.

        This is the real `finstone run` twice, including the re-stage: the
        inbox is drained after a successful import, so a retry has to put the
        file back before it can prove anything.
        """
        source = _statement_pdf(
            config.uploads_for("dummy") / "a.pdf",
            [("03 Jun", "Salary", "+2,000.00")],
            opening="1,000.00", closing="3,000.00",
        )
        registry = registry_for(source)

        stage(config, "dummy", repository=repository, context=context)
        _run(config, context, repository, blob_store, notifier, registry)
        before = repository.counts(context)

        staged_again = stage(config, "dummy", repository=repository, context=context)
        second = _run(config, context, repository, blob_store, notifier, registry)

        # stage skips it because the ledger already knows the digest, so
        # ingest has nothing to do at all.
        assert staged_again.already_known == 1 and staged_again.staged == 0
        assert second.txns_inserted == 0
        assert repository.counts(context) == before

    def test_the_inbox_is_drained_after_a_document_is_processed(
        self, config, repository, context, blob_store, notifier, registry_for
    ):
        """Otherwise the inbox grows without bound and every run reprocesses
        the entire history of everything ever dropped in."""
        path = _statement_pdf(
            config.inbox_dir / "dummy" / "a.pdf",
            [("03 Jun", "Salary", "+2,000.00")],
            opening="1,000.00", closing="3,000.00",
        )
        _run(config, context, repository, blob_store, notifier, registry_for(path))

        assert not path.exists()
        assert not list(config.inbox_dir.rglob("*.pdf"))
        # The original is safe in the content-addressed store, which is what
        # makes removing the inbox copy sound.
        assert any(f.is_file() and len(f.name) == 64 for f in config.store_dir.rglob("*"))

    def test_a_quarantined_document_is_not_re_reported_every_run(
        self, config, repository, context, blob_store, notifier
    ):
        """A failure that re-alerts on every run turns a real signal into
        noise, and eventually into something nobody reads."""
        _statement_pdf(config.inbox_dir / "dummy" / "a.pdf", [], opening="0.00", closing="0.00")
        first = _run(config, context, repository, blob_store, notifier, AdapterRegistry())
        assert first.quarantined == 1
        assert first.outcomes[0].reason == "unknown_layout"

        # Offered again, it is recognised rather than re-parsed and re-alerted.
        _statement_pdf(config.inbox_dir / "dummy" / "a.pdf", [], opening="0.00", closing="0.00")
        second = _run(config, context, repository, blob_store, notifier, AdapterRegistry())
        assert second.outcomes[0].reason == "already_quarantined"

    def test_ingest_is_scoped_to_a_profile(
        self, config, repository, context, blob_store, notifier, registry_for
    ):
        """A prod run must never reach into the dummy inbox. The prod/dummy
        boundary is worth nothing if stage enforces it and ingest ignores it."""
        dummy = _statement_pdf(
            config.inbox_dir / "dummy" / "a.pdf",
            [("03 Jun", "Salary", "+2,000.00")],
            opening="1,000.00", closing="3,000.00",
        )
        registry = registry_for(dummy)

        summary = ingest_inbox(config, context, repository, blob_store, notifier, registry, profile="prod")
        assert summary.processed == 0
        assert dummy.exists()          # untouched
        assert repository.counts(context).documents == 0

        summary = ingest_inbox(config, context, repository, blob_store, notifier, registry, profile="dummy")
        assert summary.imported == 1

    def test_stores_the_original_content_addressed(self, config, repository, context, blob_store, notifier, registry_for):
        path = _statement_pdf(config.inbox_dir / "dummy" / "a.pdf", [], opening="0.00", closing="0.00")
        _run(config, context, repository, blob_store, notifier, registry_for(path))
        stored = list(config.store_dir.rglob("*"))
        assert any(f.is_file() and len(f.name) == 64 for f in stored)

    def test_reconciliation_failure_writes_no_transactions(
        self, config, repository, context, blob_store, notifier, registry_for
    ):
        """A dropped row must reject the whole document, not import part of it."""
        path = _statement_pdf(
            config.inbox_dir / "dummy" / "a.pdf",
            [("03 Jun", "Salary", "+2,000.00")],
            opening="1,000.00", closing="9,999.00",   # deliberately wrong
        )
        summary = _run(config, context, repository, blob_store, notifier, registry_for(path))

        assert summary.quarantined == 1 and summary.imported == 0
        counts = repository.counts(context)
        assert counts.txns == 0 and counts.documents == 0

        reason_files = list(config.quarantine_dir_for("dummy").glob(f"*{REASON_SUFFIX}"))
        assert len(reason_files) == 1
        reason = json.loads(reason_files[0].read_text(encoding="utf-8"))
        assert reason["failure_class"] == "validation_failed"
        detail = reason["detail"]["failures"][0]["detail"]
        assert detail["expected_closing_minor"] == 300000
        assert detail["stated_closing_minor"] == 999900

    def test_unknown_layout_is_quarantined_never_guessed(
        self, config, repository, context, blob_store, notifier
    ):
        _statement_pdf(config.inbox_dir / "dummy" / "a.pdf", [], opening="0.00", closing="0.00")
        summary = _run(config, context, repository, blob_store, notifier, AdapterRegistry())

        assert summary.quarantined == 1
        reason = json.loads(next(config.quarantine_dir_for("dummy").glob(f"*{REASON_SUFFIX}")).read_text(encoding="utf-8"))
        assert reason["failure_class"] == "unknown_layout"
        # The computed fingerprint is recorded so registering it is a one-liner.
        assert len(reason["detail"]["fingerprint"]) == 40

    def test_same_day_identical_amounts_both_survive(
        self, config, repository, context, blob_store, notifier, registry_for
    ):
        path = _statement_pdf(
            config.inbox_dir / "dummy" / "a.pdf",
            [("12 Jun", "Koufu", "4.50"), ("12 Jun", "Koufu", "4.50")],
            opening="100.00", closing="91.00",
        )
        summary = _run(config, context, repository, blob_store, notifier, registry_for(path))
        assert summary.imported == 1
        assert repository.counts(context).txns == 2

    def test_quarantine_does_not_stop_the_run(
        self, config, repository, context, blob_store, notifier, registry_for
    ):
        good = _statement_pdf(
            config.inbox_dir / "dummy" / "good.pdf",
            [("03 Jun", "Salary", "+2,000.00")],
            opening="1,000.00", closing="3,000.00",
        )
        _statement_pdf(
            config.inbox_dir / "dummy" / "bad.pdf",
            [("03 Jun", "Salary", "+2,000.00")],
            opening="1,000.00", closing="1.00",
            strapline="SYNTHETIC TEST STATEMENT",
        )
        summary = _run(config, context, repository, blob_store, notifier, registry_for(good))
        assert summary.imported == 1 and summary.quarantined == 1
        assert repository.counts(context).txns == 1


class TestReparse:
    """Replaying originals from the store is the counterpart to draining the
    inbox: once the inbox copy is gone, the store is the only way back."""

    def _quarantine_one(self, config, repository, context, blob_store, notifier):
        path = _statement_pdf(
            config.inbox_dir / "dummy" / "a.pdf",
            [("03 Jun", "Salary", "+2,000.00")],
            opening="1,000.00", closing="3,000.00",
        )
        registry_with_adapter = AdapterRegistry()
        registry_with_adapter.register(SyntheticAdapter(), synthetic_signature())
        # Ingest with an empty registry so it quarantines as an unknown layout.
        _run(config, context, repository, blob_store, notifier, AdapterRegistry())
        return registry_with_adapter

    def test_replays_a_quarantined_document_once_an_adapter_exists(
        self, config, repository, context, blob_store, notifier
    ):
        fixed = self._quarantine_one(config, repository, context, blob_store, notifier)
        assert repository.counts(context).documents == 0
        assert not list(config.inbox_dir.rglob("*.pdf"))   # inbox already drained

        summary = reparse(config, context, repository, blob_store, notifier, fixed, quarantined_only=True)

        assert summary.imported == 1
        counts = repository.counts(context)
        assert counts.documents == 1 and counts.txns == 1
        assert counts.documents_quarantined == 0

    def test_clears_the_stale_reason_file(
        self, config, repository, context, blob_store, notifier
    ):
        """A reason describing a failure that has since been fixed makes
        `finstone report` lie about the current state."""
        fixed = self._quarantine_one(config, repository, context, blob_store, notifier)
        assert list(config.quarantine_dir_for("dummy").glob(f"*{REASON_SUFFIX}"))

        reparse(config, context, repository, blob_store, notifier, fixed, quarantined_only=True)
        assert not list(config.quarantine_dir_for("dummy").glob(f"*{REASON_SUFFIX}"))

    def test_clears_the_exported_original_too(
        self, config, repository, context, blob_store, notifier
    ):
        """An exported original is the same stale claim in a form someone can
        double-click, so it goes when the reason does."""
        from app.pipeline import quarantine

        fixed = self._quarantine_one(config, repository, context, blob_store, notifier)
        exported = quarantine.export_originals(
            config.quarantine_dir_for("dummy"), config.quarantine_files_dir_for("dummy"),
            blob_store,
        )
        assert len(exported) == 1

        reparse(config, context, repository, blob_store, notifier, fixed, quarantined_only=True)
        assert not list(config.quarantine_files_dir_for("dummy").iterdir())

    def test_replacing_an_imported_document_does_not_duplicate_it(
        self, config, repository, context, blob_store, notifier, registry_for
    ):
        path = _statement_pdf(
            config.inbox_dir / "dummy" / "a.pdf",
            [("03 Jun", "Salary", "+2,000.00")],
            opening="1,000.00", closing="3,000.00",
        )
        registry = registry_for(path)
        _run(config, context, repository, blob_store, notifier, registry)
        before = repository.counts(context)

        summary = reparse(config, context, repository, blob_store, notifier, registry)
        assert summary.imported == 1
        assert repository.counts(context) == before

    def test_unknown_digest_is_an_error_not_a_silent_no_op(
        self, config, repository, context, blob_store, notifier
    ):
        with pytest.raises(LookupError):
            reparse(config, context, repository, blob_store, notifier,
                    AdapterRegistry(), sha256="f" * 64)

    def test_provenance_survives_a_reparse(
        self, config, repository, context, blob_store, notifier
    ):
        """A reparse reads from the store, where the filename is a digest. If
        provenance were re-derived from that path, the operator's folder would
        be replaced by a hash and `status` would group failures by sha256."""
        _statement_pdf(
            config.inbox_dir / "dummy" / "MyBank" / "acc" / "june.pdf",
            [("03 Jun", "Salary", "+2,000.00")],
            opening="1,000.00", closing="3,000.00",
        )
        _run(config, context, repository, blob_store, notifier, AdapterRegistry())

        before = repository.list_documents(context)[0]
        assert before["source_relpath"] == "MyBank/acc/june.pdf"

        reparse(config, context, repository, blob_store, notifier,
                AdapterRegistry(), quarantined_only=True)

        after = repository.list_documents(context)[0]
        assert after["source_relpath"] == "MyBank/acc/june.pdf"
        assert after["source_profile"] == "dummy"
        assert after["institution"] == "MyBank"


class TestValidation:
    def _document(self, txns, opening, closing):
        return ParsedDocument(
            institution="Test", doc_type="acc",
            period_start=date(2024, 6, 1), period_end=date(2024, 6, 30),
            parser_version="test@1",
            accounts=(ParsedAccount(
                account_ref_masked="x", currency="SGD", kind=DEPOSIT,
                txns=tuple(txns), opening_balance_minor=opening, closing_balance_minor=closing,
            ),),
        )

    def _txn(self, day, amount):
        return ParsedTxn(posted_date=date(2024, 6, day), amount_minor=amount,
                         currency="SGD", description_raw="x")

    def test_balances_that_reconcile_pass(self):
        doc = self._document([self._txn(3, 20000), self._txn(10, -15000)], 100000, 105000)
        assert validate(doc, amount_ceiling_minor=10**9).ok

    def test_balances_that_do_not_reconcile_fail(self):
        doc = self._document([self._txn(3, 20000)], 100000, 999999)
        result = validate(doc, amount_ceiling_minor=10**9)
        assert not result.ok
        assert result.failures[0].check == "balance_reconciliation"

    def test_missing_balances_are_unverified_not_reconciled(self):
        """A format that states no balances is not the same as one that
        reconciled. Collapsing the two would discard the only end-to-end
        correctness guarantee the system has."""
        doc = self._document([self._txn(3, 20000)], None, None)
        result = validate(doc, amount_ceiling_minor=10**9)
        assert result.ok
        assert result.status == STATUS_IMPORTED_UNVERIFIED
        assert result.unverified_accounts

    def test_dates_outside_the_period_fail(self):
        doc = self._document([ParsedTxn(posted_date=date(2024, 8, 1), amount_minor=0,
                                        currency="SGD", description_raw="x")], 0, 0)
        checks = {f.check for f in validate(doc, amount_ceiling_minor=10**9).failures}
        assert "dates_within_period" in checks

    def test_amounts_above_the_ceiling_fail(self):
        doc = self._document([self._txn(3, 10**12)], 0, 10**12)
        checks = {f.check for f in validate(doc, amount_ceiling_minor=10**9).failures}
        assert "amount_ceiling" in checks

    def test_out_of_order_dates_fail(self):
        doc = self._document([self._txn(10, 0), self._txn(3, 0)], 0, 0)
        checks = {f.check for f in validate(doc, amount_ceiling_minor=10**9).failures}
        assert "date_monotonicity" in checks

    def test_ordering_by_transaction_date_is_accepted(self):
        """Card statements are ordered by transaction date, so posting dates
        legitimately go backwards: bought 29 Sep, posted 3 Oct; bought 30 Sep,
        posted 2 Oct. Requiring monotonic posting dates rejected eleven real
        statements that reconciled to the cent."""
        txns = (
            ParsedTxn(posted_date=date(2024, 6, 3), value_date=date(2024, 5, 29),
                      amount_minor=0, currency="SGD", description_raw="a"),
            ParsedTxn(posted_date=date(2024, 6, 2), value_date=date(2024, 5, 30),
                      amount_minor=0, currency="SGD", description_raw="b"),
        )
        doc = self._document(list(txns), 0, 0)
        result = validate(doc, amount_ceiling_minor=10**9)
        assert "date_monotonicity" not in {f.check for f in result.failures}

    def test_disorder_in_both_orderings_still_fails(self):
        """Loosening the check must not disable it."""
        txns = (
            ParsedTxn(posted_date=date(2024, 6, 10), value_date=date(2024, 6, 10),
                      amount_minor=0, currency="SGD", description_raw="a"),
            ParsedTxn(posted_date=date(2024, 6, 3), value_date=date(2024, 6, 3),
                      amount_minor=0, currency="SGD", description_raw="b"),
        )
        doc = self._document(list(txns), 0, 0)
        assert "date_monotonicity" in {f.check for f in validate(doc, amount_ceiling_minor=10**9).failures}


class TestRegistry:
    """Routing matches on the header lines an adapter declares, not on an
    exact hash of everything in the header band."""

    def _document(self, tmp_path, **kwargs):
        path = _statement_pdf(tmp_path / "s.pdf", [], closing="1,000.00", **kwargs)
        return pdfio.load(path)

    def test_a_declared_layout_is_matched(self, tmp_path):
        registry = AdapterRegistry()
        adapter = SyntheticAdapter()
        registry.register(adapter, synthetic_signature())
        assert registry.resolve(self._document(tmp_path)) is adapter

    def test_extra_header_lines_do_not_break_matching(self, tmp_path):
        """The real failure this replaced: a customer moving house added
        'EXAMPLE ROAD' to the header band and invalidated the layout."""
        registry = AdapterRegistry()
        registry.register(SyntheticAdapter(), synthetic_signature())
        moved = self._document(tmp_path, extra_header="EXAMPLE ROAD")
        assert registry.resolve(moved).name == "test.synthetic"

    def test_a_missing_required_line_does_not_match(self, tmp_path):
        registry = AdapterRegistry()
        registry.register(SyntheticAdapter(), synthetic_signature())
        with pytest.raises(UnknownLayout):
            registry.resolve(self._document(tmp_path, strapline="SOMETHING ELSE ENTIRELY"))

    def test_two_adapters_claiming_one_document_is_an_error(self, tmp_path):
        """Never a coin toss: if two signatures both match, they are wrong and
        that needs fixing rather than one winning arbitrarily."""
        registry = AdapterRegistry()
        registry.register(SyntheticAdapter(), synthetic_signature())
        registry.register(SyntheticAdapter(), synthetic_signature())
        with pytest.raises(AmbiguousLayout):
            registry.resolve(self._document(tmp_path))

    def test_explain_names_the_missing_line(self, tmp_path):
        """A routing failure has to say *why*, or every one needs the file."""
        registry = AdapterRegistry()
        registry.register(SyntheticAdapter(), synthetic_signature())
        candidates = registry.explain(self._document(tmp_path, strapline="SOMETHING ELSE"))
        assert candidates[0]["matches"] is False
        assert "synthetic test statement" in candidates[0]["missing"]

    def test_a_signature_must_require_something(self):
        from app.parsers.fingerprint import LayoutSignature
        with pytest.raises(ValueError):
            LayoutSignature(requires=())
