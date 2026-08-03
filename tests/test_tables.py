"""The shared table engine.

Each case here is a property measured off a real statement, not invented. The
geometry in the fixtures — column edges, row pitch, wrap distances — comes from
the four institutions surveyed before the engine was written.
"""

from __future__ import annotations

import pytest

from app.parsers import tables
from app.parsers.pdfio import Line, Word


def _line(words, top=100.0, page=1):
    return Line(top, tuple(Word(text, x0, x1, top) for text, x0, x1 in words), page)


def _dbs_spec(**kwargs):
    """The DBS savings geometry, measured off the statement."""
    header = _line([
        ("Date", 45.4, 64.9), ("Description", 113.1, 162.6),
        ("Withdrawal", 337.9, 385.9), ("Deposit", 429.7, 462.8),
        ("Balance", 515.4, 549.9),
    ], top=178.0)
    return tables.TableSpec(
        columns=tables.columns_from_header(header, [
            ("date", "Date", tables.DATE, 0),
            ("description", "Description", tables.TEXT, 0),
            ("withdrawal", "Withdrawal", tables.MONEY, -1),
            ("deposit", "Deposit", tables.MONEY, +1),
            ("balance", "Balance", tables.BALANCE, 0),
        ]),
        **kwargs,
    )


class TestColumnAssignment:
    """Amounts are right-aligned and headings are not, so the two halves of a
    row have to be cut by different rules."""

    def test_a_right_aligned_amount_lands_in_its_own_column(self):
        spec = _dbs_spec()
        # Measured: the withdrawal starts 30pt right of its heading and the
        # balance 8pt *left* of its own. Left-edge banding puts the balance in
        # the deposit column.
        row = _line([
            ("01/12/2021", 45.4, 90.4), ("Advice", 113.1, 140.1),
            ("541.80", 367.4, 394.9), ("53,398.18", 507.7, 547.8),
        ])
        cells = spec.cells(row)
        assert cells["date"] == "01/12/2021"
        assert cells["withdrawal"] == "541.80"
        assert cells["deposit"] == ""
        assert cells["balance"] == "53,398.18"

    def test_a_deposit_lands_in_the_deposit_column(self):
        spec = _dbs_spec()
        row = _line([
            ("04/12/2021", 45.4, 90.4), ("Receipt", 211.2, 241.7),
            ("47.00", 451.5, 474.0), ("53,080.18", 507.7, 547.8),
        ])
        cells = spec.cells(row)
        assert cells["deposit"] == "47.00" and cells["withdrawal"] == ""

    def test_a_long_description_does_not_spill_into_the_money_columns(self):
        """Descriptions run well past their heading; nearest-anchor matching
        would pull their tails into the amount column."""
        spec = _dbs_spec()
        row = _line([
            ("04/12/2021", 45.4, 90.4), ("Advice", 113.1, 140.1),
            ("FAST", 142.6, 165.6), ("Payment", 168.1, 203.7),
            ("Receipt", 211.2, 241.7), ("47.00", 451.5, 474.0),
        ])
        cells = spec.cells(row)
        assert cells["description"] == "Advice FAST Payment Receipt"

    def test_margin_text_is_ignored(self):
        """DBS prints registration numbers rotated down the left margin; where
        one lands on a transaction's baseline it joined that row's date."""
        spec = _dbs_spec()
        row = _line([
            (".oN", 11.0, 20.0), ("05/12/2021", 45.4, 90.4),
            ("140.81", 367.4, 394.9), ("52,949.57", 507.7, 547.8),
        ])
        assert spec.cells(row)["date"] == "05/12/2021"


class TestRowDetection:
    def test_a_line_without_money_is_not_a_row(self):
        """What keeps section headings and page furniture out of the ledger."""
        spec = _dbs_spec()
        lines = [_line([("Deposits", 55.0, 90.0)], top=126.0)]
        assert tables.assemble_rows(lines, spec) == []

    def test_a_line_with_money_is_a_row(self):
        spec = _dbs_spec()
        lines = [_line([("01/12/2021", 45.4, 90.4), ("541.80", 367.4, 394.9)], top=252.0)]
        assert len(tables.assemble_rows(lines, spec)) == 1


class TestDescriptionWrapping:
    def test_trailing_lines_attach_to_their_own_row(self):
        """DBS trails up to four references under a transaction, the last 42pt
        down against a 48pt pitch."""
        spec = _dbs_spec(continuation_gap=None)
        lines = [
            _line([("01/12/2021", 45.4, 90.4), ("Advice", 113.1, 140.1),
                   ("541.80", 367.4, 394.9)], top=252.0),
            _line([("L00000000000000000N01", 113.1, 200.0)], top=262.5),
            _line([("OTHER", 113.1, 140.0)], top=294.0),
            _line([("05/12/2021", 45.4, 90.4), ("Debit", 113.1, 140.1),
                   ("140.81", 367.4, 394.9)], top=300.0),
        ]
        rows = tables.assemble_rows(lines, spec)
        assert len(rows) == 2
        assert "OTHER" in rows[0].description("description")
        # The next row must not inherit it.
        assert rows[1].description("description") == "Debit"

    def test_a_bounded_gap_still_treats_distant_text_as_a_lead_in(self):
        """Trust prints a merchant name *above* its row, so a finite gap has to
        keep distinguishing the two directions."""
        spec = _dbs_spec(continuation_gap=9.0)
        lines = [
            _line([("01/12/2021", 45.4, 90.4), ("First", 113.1, 140.1),
                   ("10.00", 367.4, 394.9)], top=252.0),
            _line([("MERCHANT", 113.1, 180.0)], top=294.0),   # 42pt away
            _line([("05/12/2021", 45.4, 90.4), ("20.00", 367.4, 394.9)], top=300.0),
        ]
        rows = tables.assemble_rows(lines, spec)
        assert "MERCHANT" not in rows[0].description("description")
        assert "MERCHANT" in rows[1].description("description")

    def test_lead_ins_keep_their_printed_order(self):
        spec = _dbs_spec(continuation_gap=9.0)
        lines = [
            _line([("ABOVE", 113.1, 150.0)], top=246.0),
            _line([("01/12/2021", 45.4, 90.4), ("OWN", 113.1, 140.1),
                   ("10.00", 367.4, 394.9)], top=252.0),
            _line([("BELOW", 113.1, 150.0)], top=258.0),
        ]
        row = tables.assemble_rows(lines, spec)[0]
        assert row.description("description") == "ABOVE OWN BELOW"


class TestSkipping:
    def test_skipped_lines_never_become_rows(self):
        import re

        spec = _dbs_spec()
        lines = [_line([("Page", 480.0, 500.0), ("1", 505.0, 510.0),
                        ("of", 515.0, 522.0), ("4", 527.0, 532.0)], top=760.0)]
        assert tables.assemble_rows(lines, spec, skip=re.compile(r"^\s*Page\s+\d+\s+of\s+\d+\s*$")) == []


class TestMoneyCells:
    def test_reports_only_columns_with_values(self):
        spec = _dbs_spec()
        row = tables.assemble_rows(
            [_line([("01/12/2021", 45.4, 90.4), ("541.80", 367.4, 394.9)], top=252.0)], spec
        )[0]
        cells = tables.money_cells(row, spec)
        assert [(c.name, text) for c, text in cells] == [("withdrawal", "541.80")]

    def test_the_column_carries_the_direction(self):
        spec = _dbs_spec()
        by_name = {c.name: c for c in spec.money_columns}
        assert by_name["withdrawal"].sign == -1
        assert by_name["deposit"].sign == +1


class TestHeaderResolution:
    def test_a_repeated_heading_resolves_left_to_right(self):
        """Trust prints "Amount" twice — once for FCY, once for SGD."""
        header = _line([
            ("Posting", 53.0, 85.0), ("Description", 146.0, 200.0),
            ("Amount", 367.0, 400.0), ("Amount", 475.0, 508.0),
        ])
        columns = tables.columns_from_header(header, [
            ("date", "Posting", tables.DATE, 0),
            ("description", "Description", tables.TEXT, 0),
            ("fcy", "Amount", tables.MONEY, 0),
            ("sgd", "Amount", tables.MONEY, 0),
        ])
        by_name = {c.name: c for c in columns}
        assert by_name["fcy"].left == 367.0
        assert by_name["sgd"].left == 475.0

    def test_a_missing_heading_is_an_error(self):
        header = _line([("Date", 45.0, 65.0)])
        with pytest.raises(LookupError):
            tables.columns_from_header(header, [("x", "Nonexistent", tables.TEXT, 0)])


class TestMoneyPattern:
    """A stray mark in an amount column must not make a row.

    DBS renders part of its registration text as loose characters strewn
    across the page: a line reading "geR 4 4 4 4 4" put a bare "4" in both the
    withdrawal and the deposit column, which failed nine statements on a guard
    meant to catch genuine ambiguity.
    """

    import re as _re
    PATTERN = _re.compile(r"[-+]?[\d,]*\d\.\d{2}")

    def test_a_bare_digit_is_not_money(self):
        spec = _dbs_spec(money_pattern=self.PATTERN)
        line = _line([("geR", 45.4, 60.0), ("4", 380.0, 385.0), ("4", 460.0, 465.0)], top=536.0)
        assert tables.assemble_rows([line], spec) == []

    def test_a_real_amount_still_makes_a_row(self):
        spec = _dbs_spec(money_pattern=self.PATTERN)
        line = _line([("01/12/2021", 45.4, 90.4), ("541.80", 367.4, 394.9)], top=252.0)
        assert len(tables.assemble_rows([line], spec)) == 1

    def test_money_cells_ignores_non_amounts(self):
        spec = _dbs_spec(money_pattern=self.PATTERN)
        row = tables.Row(
            _line([("x", 0, 1)]),
            {"date": "", "description": "", "withdrawal": "4", "deposit": "47.00", "balance": ""},
        )
        assert [(c.name, t) for c, t in tables.money_cells(row, spec)] == [("deposit", "47.00")]

    def test_without_a_pattern_any_text_counts(self):
        """Layouts that have not needed the restriction keep the old behaviour."""
        spec = _dbs_spec()
        line = _line([("x", 45.4, 60.0), ("4", 380.0, 385.0)], top=536.0)
        assert len(tables.assemble_rows([line], spec)) == 1
