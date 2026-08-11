"""OCBC 360 savings statement adapter.

An ordinary two-column deposit table, and the tidiest layout in the corpus: it
prints its period, both balances *and* its own totals, so three independent
checks are available on one document.

    BALANCE B/F                        1,240.55
    10 JUN  10 JUN  BONUS INTEREST         0.69   1,241.24
                    360 CC SPEND BONUS
    ...
    BALANCE C/F                        3,740.55
    Total Withdrawals/Deposits    0.00 2,500.00

Three details worth naming. A cheque number sits between the description and the
amounts, and it is text rather than money — it is left of the money columns, so
the shared engine bands it with the description without being told. The
description wraps *below* its row and only below it, sometimes onto three lines:
the bonus category that says what an interest line was for, or the card number,
channel and country under a bill payment. And OCBC posts the month-end interest
credit on the day after the period ends, inside the balance it carries forward,
so this is a layout whose last row is dated outside its own period.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.dates import (
    DEFAULT_LOOKAHEAD_DAYS,
    DateParseError,
    parse_full_date,
    resolve_near_period,
)
from ...domain.models import DEPOSIT, DOC_TYPE_ACCOUNT, ParsedAccount, ParsedDocument, ParsedTxn
from ...domain.money import AmountParseError, parse_amount
from ...ports.parser import ParseError
from .. import pdfio, tables
from ..fingerprint import LayoutSignature

INSTITUTION = "OCBC"
BASE_CURRENCY = "SGD"

#: What identifies a 360 savings statement. "Statement of account" is what
#: separates it from the card statement, which announces a credit limit
#: instead. Nothing customer-specific is required.
SIGNATURE = LayoutSignature(
    producer=("streamline", "pdfgen"),
    requires=("statement of account",),
)

POSTED_COL = "posted"
VALUE_COL = "value"
DESC_COL = "description"
CHEQUE_COL = "cheque"
OUT_COL = "withdrawal"
IN_COL = "deposit"
BALANCE_COL = "balance"

#: OCBC only ever wraps *downwards*, and it wraps more than one line: a bill
#: payment trails the card number it settled, then the channel, then the
#: country, each about 11pt under the last. `None` is "always belongs to the row
#: above", which is what DBS uses for the same reason.
#:
#: This was 14.0, a distance chosen from the one-line bonus-category wrap. The
#: shared assembler measures the gap from the *row*, not from the last line
#: attached to it, so the distance accumulates: the first wrapped line landed at
#: 11pt and attached, the second at 22pt and did not. The second was then held
#: over and prepended to the *next* row — "SALA Salary INTEREST CREDIT" — while
#: the lines between were dropped outright. Two descriptions corrupted per wrap,
#: and `description_norm` feeds the dedupe key.
CONTINUATION_GAP = None

#: OCBC posts a month-end interest credit on the following day — value date
#: 31 JUL, posting date 01 AUG — and counts it in the balance the statement
#: carries forward. The row belongs to the statement that prints it, so its
#: posting date has to be readable just past the period end.
POSTING_GRACE_DAYS = DEFAULT_LOOKAHEAD_DAYS

_AMOUNT_IN_CELL = re.compile(r"[\d,]*\d\.\d{2}")
_HEADER = re.compile(r"\bDate\b.*\bDescription\b.*\bWithdrawal\b.*\bDeposit\b", re.IGNORECASE)
_PERIOD = re.compile(
    r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\s+TO\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
    re.IGNORECASE,
)
_ACCOUNT_LINE = re.compile(r"Account\s+No\.?\s*([0-9][0-9\- ]{5,})", re.IGNORECASE)

_BROUGHT = re.compile(r"^\s*BALANCE\s+B\s*/\s*F\b", re.IGNORECASE)
_CARRIED = re.compile(r"^\s*BALANCE\s+C\s*/\s*F\b", re.IGNORECASE)
#: The statement's own arithmetic about itself, checked independently of the
#: balances: a row read into the wrong column moves both totals.
_DECLARED = re.compile(r"^\s*Total\s+Withdrawals?\s*/\s*Deposits?\b", re.IGNORECASE)

#: Where the table stops. Page 2 is a glossary of transaction codes and the
#: deposit-insurance notice, none of it table content.
_SECTION_END = re.compile(
    r"^\s*(Total\s+Withdrawals|TRANSACTION\s+CODE|Deposit\s+Insurance)", re.IGNORECASE
)


class OcbcAccountAdapter:
    name = "ocbc.acc"
    version = "1.0.0"
    institution = INSTITUTION
    doc_type = DOC_TYPE_ACCOUNT

    def parse(self, path: Path, *, password: str | None = None) -> ParsedDocument:
        document = pdfio.load(Path(path), password=password)

        period_start, period_end = self._period(document)
        header = self._header(document)
        spec = tables.TableSpec(
            columns=tables.columns_from_header(header, [
                (POSTED_COL, "Date", tables.DATE, 0),
                # The second "Date" heading. Matched left to right, so naming
                # it is what stops the value date binding to the posting one.
                (VALUE_COL, "Date", tables.DATE, 0),
                (DESC_COL, "Description", tables.TEXT, 0),
                # A cheque number, and it sits left of the money columns, so
                # the engine bands it with the description on its own.
                (CHEQUE_COL, "Cheque", tables.TEXT, 0),
                (OUT_COL, "Withdrawal", tables.MONEY, -1),
                (IN_COL, "Deposit", tables.MONEY, +1),
                (BALANCE_COL, "Balance", tables.BALANCE, 0),
            ]),
            continuation_gap=CONTINUATION_GAP,
            money_pattern=_AMOUNT_IN_CELL,
        )

        opening = closing = None
        declared = None
        txns = []

        for row in tables.assemble_rows(self._table_lines(document), spec):
            text = row.line.text
            if _BROUGHT.match(text):
                opening = opening if opening is not None else self._last_amount(row.line)
                continue
            if _CARRIED.match(text):
                closing = self._last_amount(row.line)
                continue
            if _DECLARED.match(text):
                declared = self._declared(row.line)
                continue

            txn = self._txn(row, spec, period_start, period_end)
            if txn is not None:
                txns.append(txn)

        if opening is None or closing is None:
            raise ParseError("statement is missing its brought-forward or carried-forward balance")

        account = ParsedAccount(
            account_ref_masked=self._account_ref(document),
            currency=BASE_CURRENCY,
            kind=DEPOSIT,
            txns=tuple(txns),
            opening_balance_minor=opening,
            closing_balance_minor=closing,
            declared_out_minor=declared[0] if declared else None,
            declared_in_minor=declared[1] if declared else None,
        )

        return ParsedDocument(
            institution=self.institution,
            doc_type=self.doc_type,
            period_start=period_start,
            period_end=period_end,
            statement_date=period_end,
            parser_version=f"{self.name}@{self.version}",
            accounts=(account,),
            posting_grace_days=POSTING_GRACE_DAYS,
        )

    def _date(self, text: str, period_start, period_end):
        """A row's year-less date, allowing for the month-end posting lag.

        No lookback: OCBC does not print a date earlier than the period on a
        savings statement, and leaving that end strict keeps the window narrow
        enough that "exactly one candidate" stays a real guarantee.
        """
        return resolve_near_period(
            text, period_start, period_end,
            lookback_days=0, lookahead_days=POSTING_GRACE_DAYS,
        )

    def _period(self, document):
        for line in document.lines():
            match = _PERIOD.search(line.text)
            if match:
                try:
                    return parse_full_date(match.group(1)), parse_full_date(match.group(2))
                except DateParseError as exc:
                    raise ParseError(f"cannot read the statement period: {exc}") from exc
        raise ParseError("no statement period found")

    def _header(self, document):
        for line in document.lines():
            if _HEADER.search(line.text):
                return line
        raise ParseError("no transaction table header found")

    def _account_ref(self, document) -> str:
        for line in document.lines():
            match = _ACCOUNT_LINE.search(line.text)
            if match:
                return " ".join(match.group(1).split())
        raise ParseError("no account number found")

    def _last_amount(self, line) -> int:
        found = _AMOUNT_IN_CELL.findall(line.text)
        if not found:
            raise ParseError(f"no amount on the balance row {line.text!r}", context={
                "page": line.page_number, "y": round(line.top, 1), "raw_line": line.text,
            })
        return _to_minor(found[-1], line)

    def _declared(self, line) -> tuple[int, int] | None:
        """The withdrawal and deposit totals the statement states for itself."""
        found = _AMOUNT_IN_CELL.findall(line.text)
        if len(found) < 2:
            return None
        return _to_minor(found[-2], line), _to_minor(found[-1], line)

    def _txn(self, row, spec, period_start, period_end) -> ParsedTxn | None:
        amounts = tables.money_cells(row, spec)
        if not amounts:
            return None
        if len(amounts) > 1:
            raise ParseError("a row carries both a withdrawal and a deposit", context={
                "page": row.line.page_number, "y": round(row.line.top, 1),
                "raw_line": row.line.text, "columns": dict(row.cells),
            })

        posted_text = row.cell(POSTED_COL)
        if not posted_text.strip():
            return None

        try:
            posted = self._date(posted_text, period_start, period_end)
        except DateParseError as exc:
            raise ParseError(str(exc), context={
                "page": row.line.page_number, "y": round(row.line.top, 1),
                "failed_on": "posting date", "raw_line": row.line.text,
                "columns": dict(row.cells),
            }) from exc

        value_date = None
        value_text = row.cell(VALUE_COL)
        if value_text.strip():
            try:
                value_date = self._date(value_text, period_start, period_end)
            except DateParseError:
                value_date = None

        column, text = amounts[0]
        return ParsedTxn(
            posted_date=posted,
            value_date=value_date,
            amount_minor=column.sign * _to_minor(text, row.line),
            currency=BASE_CURRENCY,
            description_raw=row.description(DESC_COL, CHEQUE_COL),
            column_sign=column.sign,
        )

    def _table_lines(self, document) -> list[pdfio.Line]:
        lines = []
        started = False
        for line in document.lines():
            if _HEADER.search(line.text):
                started = True
                continue
            if not started:
                continue
            if _SECTION_END.match(line.text):
                # The declared totals sit on the line that ends the table, so
                # they are kept rather than cut away with the rest.
                if _DECLARED.match(line.text):
                    lines.append(line)
                started = False
                continue
            lines.append(line)
        return lines


def _to_minor(text: str, line) -> int:
    try:
        minor, _ = parse_amount(text.strip(), default_currency=BASE_CURRENCY)
    except AmountParseError as exc:
        raise ParseError(f"cannot read {text!r} as an amount", context={
            "page": line.page_number, "y": round(line.top, 1), "raw_line": line.text,
        }) from exc
    return minor
