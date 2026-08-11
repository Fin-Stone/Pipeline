"""OCBC 360 savings layout handling.

The July 2026 statement quarantined as `parse_failed`, and this reconstructs it
from what the quarantine report recorded — the raw lines, their y-positions and
the column contents — plus the column x-positions measured off the June sample.
No access to the original document is needed, which is the property the
diagnostics exist to provide.

Two things went wrong on that one statement, and both are here:

- OCBC posts the month-end interest credit on the day *after* the period ends
  and counts it in the balance the statement carries forward. Read strictly
  against the printed period the row is unreadable, so the document was rejected
  over 0.18 the closing balance already accounts for.
- A bill payment trails the card number it settled, then the channel, then the
  country. Only the first attached to its row; the last was prepended to the row
  below and the middle was dropped.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.parsers.ocbc.acc import OcbcAccountAdapter
from app.pipeline.validate import validate
from app.ports.parser import ParseError

from .fixtures.make_pdf import write_pdf

# Measured off the June sample: each column's left edge, and for the
# right-aligned money columns the edge an amount is set against.
POSTED_X, VALUE_X, DESC_X = 46.2, 91.6, 136.9
CHEQUE_X, WITHDRAWAL_X, DEPOSIT_X, BALANCE_X = 233.3, 331.0, 426.4, 512.1

#: Helvetica 9pt runs about 4.6pt per digit; the money bands are ~85pt apart, so
#: placing an amount by an estimated width lands it well inside its own column.
_DIGIT_WIDTH = 4.6

#: The row pitch and wrap pitch the quarantine report recorded.
ROW_PITCH = 12.9
WRAP_PITCH = 10.8


def _right_aligned(text: str, right_edge: float) -> float:
    """Left x for `text` so its right edge lands just past `right_edge`."""
    return right_edge + 14.9 - len(text) * _DIGIT_WIDTH


def statement(
    rows: list[tuple[str, str, str, str, str, list[str]]],
    *,
    opening: str = "5,595.53",
    closing: str = "4,018.64",
    declared: tuple[str, str] = ("1,577.07", "0.18"),
    period: str = "1 JUL 2026 TO 31 JUL 2026",
) -> list[tuple[float, float, str]]:
    """Build a 360 savings statement.

    `rows` are (posted, value, description, withdrawal, deposit, wrapped lines),
    with the wrapped lines printed under the row as OCBC prints its references.
    """
    placements: list[tuple[float, float, str]] = [
        (46.2, 127.0, "STATEMENT OF ACCOUNT"),
        (46.2, 239.4, f"360 ACCOUNT {period}"),
        (46.2, 256.2, "Account No. 100200300400"),
        (46.2, 271.9, "Transaction Value"),
        (POSTED_X, 285.1, "Date"), (VALUE_X, 285.1, "Date"),
        (DESC_X, 285.1, "Description"), (CHEQUE_X, 285.1, "Cheque"),
        (WITHDRAWAL_X, 285.1, "Withdrawal"), (DEPOSIT_X, 285.1, "Deposit"),
        (BALANCE_X, 285.1, "Balance"),
    ]

    top = 301.6
    placements += [
        (DESC_X, top, "BALANCE B/F"),
        (_right_aligned(opening, 536.3), top, opening),
    ]
    top += ROW_PITCH

    for posted, value, description, withdrawal, deposit, wrapped in rows:
        placements += [(POSTED_X, top, posted), (VALUE_X, top, value),
                       (DESC_X, top, description)]
        if withdrawal:
            placements.append((_right_aligned(withdrawal, 364.1), top, withdrawal))
        if deposit:
            placements.append((_right_aligned(deposit, 449.1), top, deposit))
        wrap_top = top
        for line in wrapped:
            wrap_top += WRAP_PITCH
            placements.append((DESC_X, wrap_top, line))
        top = wrap_top + ROW_PITCH

    placements += [
        (DESC_X, top, "BALANCE C/F"),
        (_right_aligned(closing, 536.3), top, closing),
    ]
    top += 26.0
    placements += [
        (DESC_X, top, "Total Withdrawals/Deposits"),
        (_right_aligned(declared[0], 364.1), top, declared[0]),
        (_right_aligned(declared[1], 449.1), top, declared[1]),
    ]
    top += 40.0
    placements.append((46.2, top, "TRANSACTION CODE DESCRIPTION"))
    return placements


#: July 2026, as the quarantine report recorded it: a bill payment carrying a
#: three-line reference, then the interest credit posted on 1 August.
JULY_ROWS = [
    ("31 JUL", "31 JUL", "BILL PAYMENT INB", "1,577.07", "",
     ["5555555555554444", "INTERNET BANKING", "SINGAPORE"]),
    ("01 AUG", "31 JUL", "INTEREST CREDIT", "", "0.18", []),
]


def _parse(tmp_path, rows=None, **kwargs):
    path = write_pdf(tmp_path / "360.pdf", statement(rows or JULY_ROWS, **kwargs))
    return OcbcAccountAdapter().parse(path)


class TestMonthEndInterestPosting:
    """OCBC credits the month's interest with the last day of the month as its
    value date and the next day as its posting date."""

    def test_the_statement_parses(self, tmp_path):
        parsed = _parse(tmp_path)
        assert parsed.period_start == date(2026, 7, 1)
        assert parsed.period_end == date(2026, 7, 31)
        assert len(parsed.accounts[0].txns) == 2

    def test_the_interest_row_keeps_its_august_posting_date(self, tmp_path):
        """Not rewritten to the period end: the row genuinely posted on the 1st,
        and its value date is the only thing that says 31 July."""
        interest = _parse(tmp_path).accounts[0].txns[1]
        assert interest.posted_date == date(2026, 8, 1)
        assert interest.value_date == date(2026, 7, 31)
        assert interest.amount_minor == 18

    def test_the_statement_reconciles_and_validates(self, tmp_path):
        """The row is in the balance the statement carries forward, so dropping
        it would fail reconciliation by 0.18 — and keeping it used to fail the
        period check instead."""
        parsed = _parse(tmp_path)
        account = parsed.accounts[0]
        assert account.opening_balance_minor + sum(
            t.amount_minor for t in account.txns
        ) == account.closing_balance_minor
        assert validate(parsed, amount_ceiling_minor=10**9).ok

    def test_the_declared_totals_agree(self, tmp_path):
        account = _parse(tmp_path).accounts[0]
        assert (account.declared_out_minor, account.declared_in_minor) == (157707, 18)

    def test_a_date_well_past_the_period_is_still_refused(self, tmp_path):
        """The grace is days, not an open end. A row dated weeks after the
        period is a misread row, not a posting lag."""
        rows = [("20 SEP", "31 JUL", "INTEREST CREDIT", "", "0.18", [])]
        with pytest.raises(ParseError, match="does not fall inside"):
            _parse(tmp_path, rows, declared=("0.00", "0.18"), closing="5,595.71")


class TestWrappedReferences:
    def test_a_three_line_reference_stays_with_its_row(self, tmp_path):
        """Measured from the row, the gap accumulated: the card number attached
        at 10.8pt, the channel was already 21.6pt away and fell out, and the
        country was prepended to the interest row below it."""
        txns = _parse(tmp_path).accounts[0].txns
        assert txns[0].description_raw == (
            "BILL PAYMENT INB 5555555555554444 INTERNET BANKING SINGAPORE"
        )

    def test_the_row_below_inherits_none_of_it(self, tmp_path):
        txns = _parse(tmp_path).accounts[0].txns
        assert txns[1].description_raw == "INTEREST CREDIT"

    def test_the_card_number_survives(self, tmp_path):
        """A deposit statement recording a bill payment against a card number is
        the only thing connecting that payment to the card, so losing the line
        it sits on is not cosmetic."""
        txns = _parse(tmp_path).accounts[0].txns
        assert "5555555555554444" in txns[0].description_raw
