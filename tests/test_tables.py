"""The shared table engine.

Each case here is a property measured off a real statement, not invented. The
geometry in the fixtures — column edges, row pitch, wrap distances — comes from
the four institutions surveyed before the engine was written.
"""

from __future__ import annotations

import pytest

from app.parsers import tables
from app.parsers.dbs.acc import _AMOUNT_IN_CELL, _amount_text
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
            ("321.90", 367.4, 394.9), ("12,345.67", 507.7, 547.8),
        ])
        cells = spec.cells(row)
        assert cells["date"] == "01/12/2021"
        assert cells["withdrawal"] == "321.90"
        assert cells["deposit"] == ""
        assert cells["balance"] == "12,345.67"

    def test_a_deposit_lands_in_the_deposit_column(self):
        spec = _dbs_spec()
        row = _line([
            ("04/12/2021", 45.4, 90.4), ("Receipt", 211.2, 241.7),
            ("47.00", 451.5, 474.0), ("12,204.86", 507.7, 547.8),
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
            ("210.40", 367.4, 394.9), ("12,064.05", 507.7, 547.8),
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
        lines = [_line([("01/12/2021", 45.4, 90.4), ("321.90", 367.4, 394.9)], top=252.0)]
        assert len(tables.assemble_rows(lines, spec)) == 1


class TestDescriptionWrapping:
    def test_trailing_lines_attach_to_their_own_row(self):
        """DBS trails up to four references under a transaction, the last 42pt
        down against a 48pt pitch."""
        spec = _dbs_spec(continuation_gap=None)
        lines = [
            _line([("01/12/2021", 45.4, 90.4), ("Advice", 113.1, 140.1),
                   ("321.90", 367.4, 394.9)], top=252.0),
            _line([("L12345678900211129N01", 113.1, 200.0)], top=262.5),
            _line([("OTHER", 113.1, 140.0)], top=294.0),
            _line([("05/12/2021", 45.4, 90.4), ("Debit", 113.1, 140.1),
                   ("210.40", 367.4, 394.9)], top=300.0),
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

    def test_a_wrap_running_onto_several_lines_attaches_whole(self):
        """OCBC's geometry: a bill payment trails the card number it settled,
        then the channel, then the country, each ~10.8pt under the last against
        a 14pt threshold.

        The gap used to be measured from the *row*, so it accumulated: the first
        line attached at 10.8, the second was already 21.6 away and fell out.
        The lines that fell out did not vanish quietly — the last of them was
        prepended to the next row, so one wrap corrupted two descriptions.
        """
        spec = _dbs_spec(continuation_gap=14.0)
        lines = [
            _line([("31/07/2026", 45.4, 90.4), ("BILL", 113.1, 135.0),
                   ("PAYMENT", 137.0, 175.0), ("1,577.07", 367.4, 394.9)], top=454.1),
            _line([("5555555555554444", 113.1, 200.0)], top=464.9),
            _line([("INTERNET", 113.1, 160.0), ("BANKING", 162.0, 205.0)], top=475.7),
            _line([("SINGAPORE", 113.1, 170.0)], top=486.5),
            _line([("01/08/2026", 45.4, 90.4), ("INTEREST", 113.1, 160.0),
                   ("0.18", 451.5, 474.0)], top=499.4),
        ]
        rows = tables.assemble_rows(lines, spec)
        assert len(rows) == 2
        assert rows[0].description("description") == (
            "BILL PAYMENT 5555555555554444 INTERNET BANKING SINGAPORE"
        )
        # And the row below inherits none of it.
        assert rows[1].description("description") == "INTEREST"

    def test_a_fragment_on_the_previous_page_is_not_a_lead_in(self):
        """`line.top` restarts at the top of each page, so a footer at y=823 read
        as sitting "above" a row at y=158 on the page after it. OCBC card
        statements imported their own page footer as part of a merchant name."""
        spec = _dbs_spec(continuation_gap=7.0)
        lines = [
            _line([("01/12/2021", 45.4, 90.4), ("First", 113.1, 140.1),
                   ("10.00", 367.4, 394.9)], top=158.8, page=1),
            _line([("HCBS250101(000000)", 113.1, 220.0)], top=823.2, page=1),
            _line([("02/12/2021", 45.4, 90.4), ("LAZADA", 113.1, 160.0),
                   ("74.51", 367.4, 394.9)], top=158.8, page=2),
        ]
        rows = tables.assemble_rows(lines, spec)
        assert rows[1].description("description") == "LAZADA"
        assert "HCBS250101(000000)" not in rows[0].description("description")

    def test_a_continuation_does_not_cross_a_page_break(self):
        """The same guard from the other side: an unbounded gap must not let the
        top of a new page attach to the last row of the one before it."""
        spec = _dbs_spec(continuation_gap=None)
        lines = [
            _line([("01/12/2021", 45.4, 90.4), ("Advice", 113.1, 140.1),
                   ("321.90", 367.4, 394.9)], top=100.0, page=1),
            _line([("PROSE", 113.1, 150.0)], top=140.0, page=2),
        ]
        rows = tables.assemble_rows(lines, spec)
        assert rows[0].description("description") == "Advice"

    def test_text_below_the_last_row_is_dropped_not_carried_forward(self):
        """A fragment too far under its row and too far above the next belongs
        to neither, and must end up in neither."""
        spec = _dbs_spec(continuation_gap=9.0)
        lines = [
            _line([("01/12/2021", 45.4, 90.4), ("First", 113.1, 140.1),
                   ("10.00", 367.4, 394.9)], top=252.0),
            _line([("ORPHAN", 113.1, 160.0)], top=290.0),   # 38pt under, 40pt over
            _line([("05/12/2021", 45.4, 90.4), ("Second", 113.1, 145.0),
                   ("20.00", 367.4, 394.9)], top=330.0),
        ]
        rows = tables.assemble_rows(lines, spec)
        assert rows[0].description("description") == "First"
        assert rows[1].description("description") == "Second"


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
            [_line([("01/12/2021", 45.4, 90.4), ("321.90", 367.4, 394.9)], top=252.0)], spec
        )[0]
        cells = tables.money_cells(row, spec)
        assert [(c.name, text) for c, text in cells] == [("withdrawal", "321.90")]

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
        line = _line([("01/12/2021", 45.4, 90.4), ("321.90", 367.4, 394.9)], top=252.0)
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


class TestDbsPeriodAndTotals:
    """Two things the real corpus forced, both about not inventing facts."""

    def test_the_period_starts_where_the_rows_do(self):
        """DBS prints only "as at <date>" and its cycles are not calendar
        months: a September statement carries rows dated 31 August, because the
        cycle runs from the day after the previous statement. Assuming the
        first of the month rejected six correctly-read statements."""
        from datetime import date

        from app.domain.models import DEPOSIT, ParsedAccount, ParsedTxn
        from app.parsers.dbs.acc import _period_start

        account = ParsedAccount(
            account_ref_masked="x", currency="SGD", kind=DEPOSIT,
            txns=(
                ParsedTxn(posted_date=date(2025, 8, 31), amount_minor=-100,
                          currency="SGD", description_raw="a"),
                ParsedTxn(posted_date=date(2025, 9, 4), amount_minor=-100,
                          currency="SGD", description_raw="b"),
            ),
        )
        assert _period_start([account], date(2025, 9, 30)) == date(2025, 8, 31)

    def test_an_ordinary_month_still_starts_on_the_first(self):
        from datetime import date

        from app.domain.models import DEPOSIT, ParsedAccount, ParsedTxn
        from app.parsers.dbs.acc import _period_start

        account = ParsedAccount(
            account_ref_masked="x", currency="SGD", kind=DEPOSIT,
            txns=(ParsedTxn(posted_date=date(2025, 9, 4), amount_minor=-100,
                            currency="SGD", description_raw="a"),),
        )
        assert _period_start([account], date(2025, 9, 30)) == date(2025, 9, 1)

    def test_declared_totals_are_checked_by_the_validator(self):
        """As a validation failure rather than a parse error, the balance check
        still runs and the report shows the rows that disagree."""
        from datetime import date

        from app.domain.models import DEPOSIT, ParsedAccount, ParsedDocument, ParsedTxn
        from app.pipeline.validate import validate

        account = ParsedAccount(
            account_ref_masked="x", currency="SGD", kind=DEPOSIT,
            txns=(ParsedTxn(posted_date=date(2025, 9, 4), amount_minor=-60000,
                            currency="SGD", description_raw="a"),),
            opening_balance_minor=100000, closing_balance_minor=40000,
            declared_out_minor=10000,      # statement says 100.00 went out
            declared_in_minor=0,
        )
        document = ParsedDocument(
            institution="DBS", doc_type="acc",
            period_start=date(2025, 9, 1), period_end=date(2025, 9, 30),
            parser_version="t@1", accounts=(account,),
        )
        result = validate(document, amount_ceiling_minor=10**9)
        checks = {f.check for f in result.failures}
        # The balances agree, so only the declared totals catch this.
        assert checks == {"declared_totals"}

    def test_matching_declared_totals_pass(self):
        from datetime import date

        from app.domain.models import DEPOSIT, ParsedAccount, ParsedDocument, ParsedTxn
        from app.pipeline.validate import validate

        account = ParsedAccount(
            account_ref_masked="x", currency="SGD", kind=DEPOSIT,
            txns=(ParsedTxn(posted_date=date(2025, 9, 4), amount_minor=-60000,
                            currency="SGD", description_raw="a"),),
            opening_balance_minor=100000, closing_balance_minor=40000,
            declared_out_minor=60000, declared_in_minor=0,
        )
        document = ParsedDocument(
            institution="DBS", doc_type="acc",
            period_start=date(2025, 9, 1), period_end=date(2025, 9, 30),
            parser_version="t@1", accounts=(account,),
        )
        assert validate(document, amount_ceiling_minor=10**9).ok


class TestReversals:
    """A reversal is printed as a negative entry in the column it reverses.

    DBS shows a rejected transfer as "100.00" in the Withdrawal column and
    "100.00-" on the next line. Both halves of reading that were wrong: the
    trailing minus was dropped from the cell, and the adapter then took abs()
    of whatever survived. Each reversal became a second withdrawal, so a
    statement was wrong by twice the amount, every time.
    """

    def _cells(self, withdrawal):
        spec = _dbs_spec(money_pattern=_AMOUNT_IN_CELL)
        row = _line([
            ("04/12/2021", 45.4, 90.4), ("Transfer", 211.2, 241.7),
            (withdrawal, 367.9, 385.9), ("12,345.67", 507.7, 547.8),
        ])
        return spec, tables.assemble_rows([row], spec)[0]

    def test_a_trailing_minus_stays_with_its_amount(self):
        """The cell regex matched the digits and left the sign behind."""
        assert _AMOUNT_IN_CELL.findall("100.00-") == ["100.00-"]
        assert _amount_text("100.00-") == "100.00-"

    def test_a_reversal_in_the_withdrawal_column_is_money_in(self):
        from app.domain.money import parse_amount

        spec, row = self._cells("100.00-")
        column, text = tables.money_cells(row, spec)[0]
        minor, _ = parse_amount(_amount_text(text), default_currency="SGD")

        assert column.name == "withdrawal" and column.sign == -1
        # The column says out; the amount's own sign says undo that.
        assert column.sign * minor == 10000

    def test_an_ordinary_withdrawal_is_unaffected(self):
        from app.domain.money import parse_amount

        spec, row = self._cells("100.00")
        column, text = tables.money_cells(row, spec)[0]
        minor, _ = parse_amount(_amount_text(text), default_currency="SGD")
        assert column.sign * minor == -10000

    def test_a_reversal_nets_off_inside_its_own_column_total(self):
        """The statement totals its columns as printed, so a refund reduces the
        withdrawal total rather than appearing as a deposit. Counting it by the
        sign of the amount put it on the wrong side and every statement holding
        a reversal disagreed with its own arithmetic."""
        from datetime import date

        from app.domain.models import DEPOSIT, ParsedAccount, ParsedDocument, ParsedTxn
        from app.pipeline.validate import validate

        def txn(amount, column_sign):
            return ParsedTxn(posted_date=date(2025, 9, 4), amount_minor=amount,
                             currency="SGD", description_raw="a",
                             column_sign=column_sign)

        account = ParsedAccount(
            account_ref_masked="x", currency="SGD", kind=DEPOSIT,
            txns=(
                txn(-50000, -1),   # a withdrawal
                txn(+10000, -1),   # reversed, printed under Withdrawal
                txn(+20000, +1),   # a deposit
            ),
            opening_balance_minor=100000,
            closing_balance_minor=80000,
            # 500.00 out less the 100.00 put back, and 200.00 in.
            declared_out_minor=40000,
            declared_in_minor=20000,
        )
        document = ParsedDocument(
            institution="DBS", doc_type="acc",
            period_start=date(2025, 9, 1), period_end=date(2025, 9, 30),
            parser_version="t@1", accounts=(account,),
        )
        assert validate(document, amount_ceiling_minor=10**9).ok

    def test_a_layout_with_one_amount_column_still_works(self):
        """Where nothing was recorded, the amount's sign is all there is."""
        from datetime import date

        from app.domain.models import ParsedTxn
        from app.pipeline.validate import _printed_under

        out = ParsedTxn(posted_date=date(2025, 9, 4), amount_minor=-100,
                        currency="SGD", description_raw="a")
        into = ParsedTxn(posted_date=date(2025, 9, 4), amount_minor=100,
                         currency="SGD", description_raw="b")
        assert _printed_under(out) == -1 and _printed_under(into) == 1
