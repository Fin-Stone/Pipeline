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

from .fixtures.make_pdf import synthetic_signature, synthetic_statement, write_pdf
from .test_pipeline import SyntheticAdapter

from app.parsers import fingerprint as fingerprinting
from app.parsers import pdfio
from app.parsers.registry import AdapterRegistry


def _registry_for(path=None):
    registry = AdapterRegistry()
    registry.register(SyntheticAdapter(), synthetic_signature())
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


class TestRedactionCoversEveryReportPath:
    """`--redact` exists so a failure on a real statement is safe to paste.

    A path that ignores it is worse than having no redaction at all, because
    the flag implies a guarantee. The first version leaked filenames from the
    unknown-layout report, which is how real statement names ended up in a
    pasted failure report.
    """

    SENSITIVE = "2026 July Statement_1000000000000000004.pdf"

    def test_parse_failure_masks_the_filename(self):
        report = diagnostics.parse_failure_report(
            filename=self.SENSITIVE, sha256="a" * 64, message="boom", redact=True,
        )
        assert self.SENSITIVE not in report
        assert "Statement" not in report

    def test_reconciliation_masks_the_filename(self):
        report = diagnostics.reconciliation_report(
            filename=self.SENSITIVE, sha256="a" * 64, adapter="t@1", failures=[], redact=True,
        )
        assert self.SENSITIVE not in report

    def test_masking_keeps_digits_and_drops_letters(self):
        """Amounts are the evidence a failure is diagnosed from; merchant and
        file names are not."""
        assert diagnostics.mask("Trust Bank 1,234.56") == "xxxxx xxxx 1,234.56"

    def test_report_command_masks_every_filename(self, config, repository, context, blob_store, notifier, capsys):
        from app.cli import cmd_report

        write_pdf(config.inbox_dir / "dummy" / "MyBank_Statement.pdf",
                  synthetic_statement([], closing="1,000.00"))
        ingest_inbox(config, context, repository, blob_store, notifier, AdapterRegistry())

        import app.cli as cli
        original = cli.load_config
        cli.load_config = lambda: config
        try:
            cmd_report(type("Args", (), {"redact": True, "out": None})())
            out = capsys.readouterr().out
        finally:
            cli.load_config = original

        assert "MyBank" not in out and "Statement" not in out
        assert "UNROUTABLE" in out
        # The header lines it printed are document text and must be masked too.
        assert "test bank placeholder limited" not in out


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


class TestQuarantineListing:
    """`report` says why a document failed. This says *which file* it is.

    Both are needed and neither substitutes for the other: the report a run
    writes is redacted by design, and the store names originals by digest, so
    between them an operator holding a failed statement twice over had no way
    to reach it.
    """

    NAME = "MyBank_Statement.pdf"

    def _quarantine_one(self, config, repository, context, blob_store, notifier):
        write_pdf(
            config.inbox_dir / "dummy" / self.NAME,
            synthetic_statement(
                [("03 Jun", "Salary", "+2,000.00")], opening="1,000.00", closing="9,999.00",
            ),
        )
        ingest_inbox(config, context, repository, blob_store, notifier, _registry_for())
        reason = next(config.quarantine_dir.glob(f"*{REASON_SUFFIX}"))
        return json.loads(reason.read_text(encoding="utf-8"))

    def _quarantine_cmd(self, config, monkeypatch, capsys, **overrides):
        import app.cli as cli

        monkeypatch.setattr(cli, "load_config", lambda: config)
        args = {"export": False, "redact": False, **overrides}
        cli.cmd_quarantine(type("Args", (), args)())
        return capsys.readouterr().out

    def test_names_the_file_and_the_check_it_failed(
        self, config, repository, context, blob_store, notifier, monkeypatch, capsys
    ):
        self._quarantine_one(config, repository, context, blob_store, notifier)
        out = self._quarantine_cmd(config, monkeypatch, capsys)

        assert self.NAME in out
        assert "validation_failed" in out
        assert "balance_reconciliation" in out

    def test_redact_masks_the_filename(
        self, config, repository, context, blob_store, notifier, monkeypatch, capsys
    ):
        """Same guarantee every other report path carries: safe to paste."""
        self._quarantine_one(config, repository, context, blob_store, notifier)
        out = self._quarantine_cmd(config, monkeypatch, capsys, redact=True)

        assert "MyBank" not in out and "Statement" not in out
        # A check name is this codebase's own word, never the document's, so it
        # stays legible — masking it would cost the diagnosis and protect nothing.
        assert "balance_reconciliation" in out

    def test_export_writes_an_openable_copy_of_the_original(
        self, config, repository, context, blob_store, notifier, monkeypatch, capsys
    ):
        payload = self._quarantine_one(config, repository, context, blob_store, notifier)
        self._quarantine_cmd(config, monkeypatch, capsys, export=True)

        exported = list(config.quarantine_files_dir.iterdir())
        assert len(exported) == 1
        # The digest prefix ties it to its reason file; the rest is recognisable.
        assert exported[0].name == f"{payload['sha256'][:8]}-{self.NAME}"
        with blob_store.open(payload["sha256"]) as original:
            assert exported[0].read_bytes() == original.read()

    def test_exporting_twice_changes_nothing(
        self, config, repository, context, blob_store, notifier, monkeypatch, capsys
    ):
        self._quarantine_one(config, repository, context, blob_store, notifier)

        def snapshot():
            return {p.name: p.read_bytes() for p in config.quarantine_files_dir.iterdir()}

        self._quarantine_cmd(config, monkeypatch, capsys, export=True)
        first = snapshot()
        self._quarantine_cmd(config, monkeypatch, capsys, export=True)
        assert snapshot() == first

    def test_an_export_whose_reason_is_gone_is_swept_up(
        self, config, repository, context, blob_store, notifier, monkeypatch, capsys
    ):
        """An export outliving its reason file describes a failure that no
        longer exists — the same lie `clear_reason` was written to prevent."""
        self._quarantine_one(config, repository, context, blob_store, notifier)
        self._quarantine_cmd(config, monkeypatch, capsys, export=True)

        orphan = config.quarantine_files_dir / "deadbeef-AnOldStatement.pdf"
        orphan.write_bytes(b"%PDF-stale")
        self._quarantine_cmd(config, monkeypatch, capsys, export=True)

        assert not orphan.exists()
        assert len(list(config.quarantine_files_dir.iterdir())) == 1
