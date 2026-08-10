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

Two details worth naming. A cheque number sits between the description and the
amounts, and it is text rather than money — it is left of the money columns, so
the shared engine bands it with the description without being told. And the
description wraps *below* its row, carrying the bonus category that says what
an interest line was actually for, which is the part worth keeping.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.dates import DateParseError, parse_full_date, resolve_period_date
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

#: The bonus category wraps about 11pt under its row; the next row is 24pt on.
CONTINUATION_GAP = 14.0

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
            posted = resolve_period_date(posted_text, period_start, period_end)
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
                value_date = resolve_period_date(value_text, period_start, period_end)
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
