"""Shared accounts: two people uploading the same statement.

In a household where several members have access to a joint account, both
uploading its statement is the normal case, not an edge case. Identity has to
be the *statement* — one per account per period — because the file carrying it
changes bytes whenever a PDF is re-downloaded, re-saved, renamed or passed
through anything that rewrites metadata.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.domain.dedupe import statement_key
from app.parsers.registry import AdapterRegistry
from app.pipeline.ingest import ingest_inbox
from app.pipeline.quarantine import REASON_SUFFIX

from .fixtures.make_pdf import synthetic_signature, synthetic_statement, write_pdf
from .test_pipeline import SyntheticAdapter


def _registry():
    registry = AdapterRegistry()
    registry.register(SyntheticAdapter(), synthetic_signature())
    return registry


def _upload(config, name, rows, *, opening="1,000.00", closing="3,000.00", strapline=None):
    kwargs = {"strapline": strapline} if strapline else {}
    return write_pdf(
        config.inbox_dir / "dummy" / name,
        synthetic_statement(rows, opening=opening, closing=closing, **kwargs),
    )


ROWS = [("03 Jun", "Salary", "+2,000.00")]


class TestStatementKey:
    def test_same_accounts_and_period_give_the_same_key(self):
        a = statement_key("acc", date(2025, 6, 1), date(2025, 6, 30), ["01-123-4"])
        b = statement_key("acc", date(2025, 6, 1), date(2025, 6, 30), ["01-123-4"])
        assert a == b

    def test_account_order_does_not_matter(self):
        """A statement covering several accounts is the same statement however
        the adapter happened to order them."""
        a = statement_key("cc", date(2025, 6, 1), date(2025, 6, 30), ["A", "B"])
        b = statement_key("cc", date(2025, 6, 1), date(2025, 6, 30), ["B", "A"])
        assert a == b

    @pytest.mark.parametrize("changed", [
        {"doc_type": "cc"},
        {"period_start": date(2025, 7, 1)},
        {"period_end": date(2025, 7, 31)},
        {"account_refs": ["01-999-9"]},
    ])
    def test_every_component_changes_the_key(self, changed):
        base = dict(doc_type="acc", period_start=date(2025, 6, 1),
                    period_end=date(2025, 6, 30), account_refs=["01-123-4"])
        assert statement_key(**base) != statement_key(**{**base, **changed})


class TestTwoMembersUploadingOneStatement:
    def test_a_re_saved_copy_is_recognised_as_the_same_statement(
        self, config, repository, context, blob_store, notifier
    ):
        """Different bytes, same statement. Previously this imported a second
        document whose every row deduplicated away, leaving balances attached
        to nothing."""
        registry = _registry()

        _upload(config, "from_alice.pdf", ROWS)
        first = ingest_inbox(config, context, repository, blob_store, notifier, registry)

        # Bob's copy: same statement, different bytes (a differing header line
        # is enough to change the digest, as a re-download would).
        _upload(config, "from_bob.pdf", ROWS, strapline="SYNTHETIC TEST STATEMENT ")
        second = ingest_inbox(config, context, repository, blob_store, notifier, registry)

        assert first.imported == 1
        assert second.imported == 0 and second.duplicates == 1
        assert second.outcomes[0].reason == "same_statement"

        counts = repository.counts(context)
        assert counts.documents == 1, "the second upload must not become a second document"
        assert counts.txns == 1

    def test_no_document_is_left_holding_balances_and_no_rows(
        self, config, repository, context, blob_store, notifier
    ):
        """The symptom that made this necessary."""
        registry = _registry()
        _upload(config, "a.pdf", ROWS)
        ingest_inbox(config, context, repository, blob_store, notifier, registry)
        _upload(config, "b.pdf", ROWS, strapline="SYNTHETIC TEST STATEMENT ")
        ingest_inbox(config, context, repository, blob_store, notifier, registry)

        from sqlalchemy import func, select
        from app.storage import schema

        with repository.engine.connect() as conn:
            for document_id in conn.execute(select(schema.source_document.c.id)).scalars():
                rows = conn.execute(
                    select(func.count()).select_from(schema.txn)
                    .where(schema.txn.c.source_document_id == document_id)
                ).scalar_one()
                balances = conn.execute(
                    select(func.count()).select_from(schema.statement_balance)
                    .where(schema.statement_balance.c.source_document_id == document_id)
                ).scalar_one()
                assert not (balances and not rows), "document has balances but no transactions"

    def test_byte_identical_upload_is_still_a_no_op(
        self, config, repository, context, blob_store, notifier
    ):
        registry = _registry()
        _upload(config, "a.pdf", ROWS)
        ingest_inbox(config, context, repository, blob_store, notifier, registry)
        _upload(config, "a-again.pdf", ROWS)
        second = ingest_inbox(config, context, repository, blob_store, notifier, registry)
        assert second.duplicates == 1
        assert repository.counts(context).documents == 1


class TestConflictingStatement:
    def test_same_period_different_transactions_is_quarantined(
        self, config, repository, context, blob_store, notifier
    ):
        """A reissued or corrected statement is a real event. Silently keeping
        either version would be a guess about which one is true."""
        registry = _registry()

        _upload(config, "original.pdf", ROWS)
        ingest_inbox(config, context, repository, blob_store, notifier, registry)

        _upload(config, "corrected.pdf",
                [("03 Jun", "Salary", "+2,000.00"), ("10 Jun", "Refund", "+500.00")],
                closing="3,500.00")
        second = ingest_inbox(config, context, repository, blob_store, notifier, registry)

        assert second.quarantined == 1
        assert second.outcomes[0].reason == "conflicting_statement"
        # The original is untouched: a conflict is reported, never resolved.
        assert repository.counts(context).txns == 1

    def test_the_conflict_report_says_how_they_differ(
        self, config, repository, context, blob_store, notifier
    ):
        registry = _registry()
        _upload(config, "original.pdf", ROWS)
        ingest_inbox(config, context, repository, blob_store, notifier, registry)
        _upload(config, "corrected.pdf",
                [("03 Jun", "Salary", "+2,000.00"), ("10 Jun", "Refund", "+500.00")],
                closing="3,500.00")
        ingest_inbox(config, context, repository, blob_store, notifier, registry)

        payload = next(
            json.loads(f.read_text(encoding="utf-8"))
            for f in config.quarantine_dir_for("dummy").glob(f"*{REASON_SUFFIX}")
            if json.loads(f.read_text(encoding="utf-8"))["failure_class"] == "conflicting_statement"
        )
        detail = payload["detail"]
        assert detail["already_held_as"] == "original.pdf"
        assert detail["transactions_here"] == 2 and detail["transactions_there"] == 1
        assert detail["only_here"] == 1
