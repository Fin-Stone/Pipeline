"""Failure reports must be diagnosable without the statement.

Each test asserts the property that actually matters: the report contains what
someone needs to fix the adapter, and `--redact` keeps it safe to paste from a
real document.
"""

from __future__ import annotations

import json

import pytest

from app.pipeline import diagnostics
from app.pipeline.ingest import ingest_inbox
from app.pipeline.quarantine import REASON_SUFFIX

from .fixtures.make_pdf import synthetic_statement, write_pdf
from .test_pipeline import SyntheticAdapter

from app.parsers import fingerprint as fingerprinting
from app.parsers import pdfio
from app.parsers.registry import AdapterRegistry


def _registry_for(path):
    registry = AdapterRegistry()
    registry.register(SyntheticAdapter(), [fingerprinting.fingerprint_pdf(pdfio.load(path))])
    return registry


class TestReconciliationReport:
    ACCOUNTS = {
        "acct/Main": [
            {"posted_date": "2026-01-03", "description": "Salary", "amount_minor": 200000},
            {"posted_date": "2026-01-20", "description": "Netflix subscription", "amount_minor": -12000},
        ]
    }
    FAILURE = [{
        "account": "acct/Main", "check": "balance_reconciliation",
        "detail": {"opening_minor": 100000, "transactions_sum_minor": 188000,
                   "expected_closing_minor": 288000, "stated_closing_minor": 300000,
                   "difference_minor": -12000, "txn_count": 2},
    }]

    def _report(self, **kwargs):
        return diagnostics.reconciliation_report(
            filename="stmt.pdf", sha256="a" * 64, adapter="test@1",
            failures=self.FAILURE, accounts=self.ACCOUNTS, **kwargs
        )

    def test_shows_the_arithmetic_not_just_a_verdict(self):
        report = self._report()
        for expected in ("1,000.00", "1,880.00", "2,880.00", "3,000.00", "-120.00"):
            assert expected in report

    def test_names_the_row_matching_the_difference(self):
        """The difference is very often exactly one transaction, which points
        straight at the row that was missed, duplicated or mis-signed."""
        report = self._report()
        assert "Netflix subscription" in report
        assert "counted twice, or carries the wrong sign" in report

    def test_says_when_no_row_matches(self):
        failure = [dict(self.FAILURE[0], detail=dict(self.FAILURE[0]["detail"], difference_minor=-777))]
        report = diagnostics.reconciliation_report(
            filename="stmt.pdf", sha256="a" * 64, adapter="test@1",
            failures=failure, accounts=self.ACCOUNTS,
        )
        assert "No parsed row matches the difference" in report

    def test_states_that_nothing_was_written(self):
        assert "No transactions were written" in self._report()

    def test_redaction_keeps_numbers_and_drops_descriptions(self):
        """Amounts are the evidence and must survive; merchant names need not."""
        report = self._report(redact=True)
        assert "Netflix subscription" not in report
        assert "-120.00" in report and "1,000.00" in report


class TestParseFailureReport:
    def test_carries_the_line_and_its_column_split(self):
        report = diagnostics.parse_failure_report(
            filename="stmt.pdf", sha256="b" * 64,
            message="'01 Jan' does not fall inside the statement period",
            adapter="trust.acc@1.0.0", fingerprint="f" * 40,
            context={
                "page": 2, "y": 222.0, "failed_on": "posting date",
                "raw_line": "01 Jan Interest +153.45",
                "columns": {"date": "01 Jan", "description": "Interest", "fcy": "", "sgd": "+153.45"},
            },
            neighbourhood=diagnostics.Neighbourhood(
                lines=((2, 194.0, "Posting date Description"), (2, 222.0, "01 Jan Interest +153.45")),
                highlight_y=222.0,
            ),
        )
        assert "page 2" in report
        assert "01 Jan Interest +153.45" in report
        assert "date='01 Jan'" in report and "sgd='+153.45'" in report
        assert "<== here" in report
        assert "failed on   posting date" in report


class TestQuarantineRecordsEnoughToDebug:
    def test_reconciliation_failure_records_the_parsed_rows(
        self, config, repository, context, blob_store, notifier
    ):
        """Without the parsed rows in the reason file, the report cannot point
        at the offending transaction later."""
        path = write_pdf(
            config.inbox_dir / "dummy" / "a.pdf",
            synthetic_statement(
                [("03 Jun", "Salary", "+2,000.00"), ("20 Jun", "Netflix", "120.00")],
                opening="1,000.00", closing="9,999.00",
            ),
        )
        ingest_inbox(config, context, repository, blob_store, notifier, _registry_for(path))

        payload = json.loads(next(config.quarantine_dir.glob(f"*{REASON_SUFFIX}")).read_text(encoding="utf-8"))
        assert payload["failure_class"] == "validation_failed"
        rows = payload["detail"]["accounts"]["01-1234567-8/Main Account"]
        assert {r["description"] for r in rows} == {"Salary", "Netflix"}

        report = diagnostics.reconciliation_report(
            filename="a.pdf", sha256=payload["sha256"],
            adapter=payload["detail"]["adapter"],
            failures=payload["detail"]["failures"],
            accounts=payload["detail"]["accounts"],
        )
        assert "difference" in report

    def test_unknown_layout_records_the_fingerprint_to_register(
        self, config, repository, context, blob_store, notifier
    ):
        write_pdf(config.inbox_dir / "dummy" / "a.pdf", synthetic_statement([], closing="1,000.00"))
        ingest_inbox(config, context, repository, blob_store, notifier, AdapterRegistry())

        payload = json.loads(next(config.quarantine_dir.glob(f"*{REASON_SUFFIX}")).read_text(encoding="utf-8"))
        assert len(payload["detail"]["fingerprint"]) == 40
        # The header lines the fingerprint was derived from, so a mismatch
        # between two months can be compared without opening either file.
        assert payload["detail"]["label_lines"]
