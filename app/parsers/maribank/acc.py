"""MariBank deposit-and-investment statement adapter.

The document covers three products; this reads the savings account and
deliberately reads nothing else.

**Why skipping the rest is not dropping data.** MariBank debits savings to buy
a fund, and prints that debit in the savings table — the same 1,000.00 appears
as an outgoing under `SAVINGS - TRANSACTION DETAILS` and again, with its unit
count, under `INVESTMENTS - TRANSACTION DETAILS`. The cash movement is already
captured; what the investment section adds is units, unit prices and a
valuation, none of which the ledger models and all of which would need schema
work to hold honestly. An investment is treated as an expense when bought and
as income when sold, which is exactly what the savings side already records.
Fixed deposits are the same shape.

So the skip is by name and it is total: sections are recognised explicitly, and
one this adapter has never seen is a `ParseError` rather than a silent pass.
That is the rule the DBS work established and it is what stops a future
MariBank product from disappearing.

**Interest is taken daily.** The interest table lists one row per day, and the
transaction table sometimes also carries a single month-level posting of the
same money — the February statement does, the August one does not. Recording
both would double-count, so the aggregate row is dropped and the daily rows
kept, which is the resolution the statement published and is uniform across
both formats.
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

INSTITUTION = "MariBank"
BASE_CURRENCY = "SGD"

#: No producer or creator: MariBank ships neither, so the header lines carry
#: routing alone. Both are the bank's own words.
SIGNATURE = LayoutSignature(
    requires=(
        "deposit and investment statement",
        "savings account summary",
    ),
)

DATE_COL = "date"
DESC_COL = "description"
OUT_COL = "outgoing"
IN_COL = "incoming"
BALANCE_COL = "balance"
INTEREST_COL = "interest"

#: The reference under a row sits about 12pt below it; the next row is 26pt
#: further on, so this separates a wrap from a new row with room to spare.
CONTINUATION_GAP = 14.0

_AMOUNT_IN_CELL = re.compile(r"[\d,]*\d\.\d{2}")
_PERIOD = re.compile(r"STATEMENT\s+PERIOD:\s*(.+?)\s+to\s+(.+?)\s*$", re.IGNORECASE)
_ACCOUNT_LINE = re.compile(r"ACCOUNT:\s*([0-9][0-9\s-]{5,})", re.IGNORECASE)

#: A section heading, e.g. "SAVINGS - TRANSACTION DETAILS". The trailing star on
#: the interest heading is a footnote marker, not part of the name.
_SECTION = re.compile(r"^(SAVINGS|FIXED DEPOSIT|INVESTMENTS)\s*-\s*(.+?)\*?\s*$", re.IGNORECASE)

#: The savings summary row: account, starting, outgoing, incoming, ending.
_SUMMARY = re.compile(r"^SAVINGS\s+[\d,]+\.\d{2}", re.IGNORECASE)

TRANSACTIONS = "Savings - Transaction Details"
INTEREST = "Savings - Interest Details"

#: Read, and read for different reasons. Anything not named here raises.
_SAVINGS_SECTIONS = {"transaction details": TRANSACTIONS, "interest details": INTEREST}
#: Recognised and skipped: see the module docstring for why this loses nothing.
_OTHER_PRODUCTS = ("fixed deposit", "investments")
#: Summaries repeat figures the detail tables carry; only the savings one is
#: read, and that is read as balances rather than as rows.
_SUMMARY_SECTIONS = ("account summary",)

_NOTHING_HERE = re.compile(r"^\s*NO (RECENT )?TRANSACTION", re.IGNORECASE)
#: A month with no day: the aggregate posting whose daily breakdown is the
#: interest table. Keeping both would count the same money twice.
_MONTH_ONLY = re.compile(r"^\s*[A-Za-z]{3,9}\s*$")


class MariBankAccountAdapter:
    name = "maribank.acc"
    version = "1.0.0"
    institution = INSTITUTION
    doc_type = DOC_TYPE_ACCOUNT

    def parse(self, path: Path, *, password: str | None = None) -> ParsedDocument:
        document = pdfio.load(Path(path), password=password)

        period_start, period_end = self._period(document)
        opening, out_total, in_total, closing = self._summary(document)

        sections = self._sections(document)
        txns = [
            *self._transactions(sections.get(TRANSACTIONS, []), period_start, period_end),
            *self._interest(sections.get(INTEREST, []), period_start, period_end),
        ]

        account = ParsedAccount(
            account_ref_masked=self._account_ref(document),
            currency=BASE_CURRENCY,
            kind=DEPOSIT,
            txns=tuple(txns),
            opening_balance_minor=opening,
            closing_balance_minor=closing,
            declared_out_minor=out_total,
            declared_in_minor=in_total,
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

    def _summary(self, document):
        """Opening, declared totals and closing, from the savings summary row.

        The totals are kept as declared figures rather than used: they are the
        statement's own arithmetic about itself, and checking the rows against
        them catches a row read into the wrong column.
        """
        for line in document.lines():
            if not _SUMMARY.match(line.text.strip()):
                continue
            amounts = _AMOUNT_IN_CELL.findall(line.text)
            if len(amounts) != 4:
                raise ParseError(
                    f"savings summary has {len(amounts)} figures, expected 4",
                    context={"page": line.page_number, "y": round(line.top, 1),
                             "raw_line": line.text},
                )
            starting, outgoing, incoming, ending = (_to_minor(a, line) for a in amounts)
            return starting, outgoing, incoming, ending
        raise ParseError("no savings account summary row found")

    def _account_ref(self, document) -> str:
        for line in document.lines():
            match = _ACCOUNT_LINE.search(line.text)
            if match:
                return " ".join(match.group(1).split())
        raise ParseError("no account number found")

    def _sections(self, document) -> dict[str, list]:
        """Lines grouped under the headings the statement prints.

        An unrecognised section raises rather than being ignored: a product
        this adapter has never seen must not vanish from a document that then
        reports itself as fully imported.
        """
        grouped: dict[str, list] = {}
        current = None

        for line in document.lines():
            match = _SECTION.match(line.text.strip())
            if match:
                current = self._classify(match.group(1), match.group(2), line)
                continue
            if current is not None:
                grouped.setdefault(current, []).append(line)
        return grouped

    def _classify(self, product: str, name: str, line) -> str | None:
        product, name = product.lower(), " ".join(name.lower().split())

        if product.startswith(_OTHER_PRODUCTS) or name in _SUMMARY_SECTIONS:
            return None
        if product == "savings" and name in _SAVINGS_SECTIONS:
            return _SAVINGS_SECTIONS[name]

        raise ParseError(
            f"unknown statement section {product.title()} - {name.title()}",
            context={"page": line.page_number, "y": round(line.top, 1),
                     "raw_line": line.text, "failed_on": "section heading"},
        )

    def _transactions(self, lines, period_start, period_end) -> list[ParsedTxn]:
        if not lines or any(_NOTHING_HERE.match(line.text) for line in lines):
            return []

        header = self._header(lines, r"\bDATE\b.*\bOUTGOING\b.*\bINCOMING\b")
        spec = tables.TableSpec(
            columns=tables.columns_from_header(header, [
                (DATE_COL, "Date", tables.DATE, 0),
                (DESC_COL, "Transaction", tables.TEXT, 0),
                (OUT_COL, "Outgoing", tables.MONEY, -1),
                (IN_COL, "Incoming", tables.MONEY, +1),
            ]),
            continuation_gap=CONTINUATION_GAP,
            money_pattern=_AMOUNT_IN_CELL,
        )

        txns = []
        for row in tables.assemble_rows(lines, spec):
            amounts = tables.money_cells(row, spec)
            if not amounts:
                continue
            if len(amounts) > 1:
                raise ParseError("a row carries both an outgoing and an incoming", context={
                    "page": row.line.page_number, "y": round(row.line.top, 1),
                    "raw_line": row.line.text, "columns": dict(row.cells),
                })

            date_text = row.cell(DATE_COL)
            if _MONTH_ONLY.match(date_text):
                # The month's interest as one posting. Its daily breakdown is
                # the interest table, and that is what gets recorded.
                continue

            column, text = amounts[0]
            txns.append(ParsedTxn(
                posted_date=self._date(date_text, row.line, period_start, period_end),
                amount_minor=column.sign * _to_minor(text, row.line),
                currency=BASE_CURRENCY,
                description_raw=row.description(DESC_COL),
                column_sign=column.sign,
                section=TRANSACTIONS,
            ))
        return txns

    def _interest(self, lines, period_start, period_end) -> list[ParsedTxn]:
        """One transaction per day of accrued interest.

        The previous-day balance beside each figure is a running balance, not a
        movement, so it is declared as one and never read as an amount.
        """
        if not lines:
            return []

        header = self._header(lines, r"\bDATE\b.*\bPREVIOUS\b.*\bINTEREST\b")
        spec = tables.TableSpec(
            columns=tables.columns_from_header(header, [
                (DATE_COL, "Date", tables.DATE, 0),
                (BALANCE_COL, "Previous", tables.BALANCE, 0),
                (INTEREST_COL, "Interest", tables.MONEY, +1),
            ]),
            money_pattern=_AMOUNT_IN_CELL,
        )

        txns = []
        for row in tables.assemble_rows(lines, spec):
            text = row.cell(INTEREST_COL)
            date_text = row.cell(DATE_COL)
            if not spec.is_money(text) or not date_text.strip():
                continue
            txns.append(ParsedTxn(
                posted_date=self._date(date_text, row.line, period_start, period_end),
                amount_minor=_to_minor(text, row.line),
                currency=BASE_CURRENCY,
                description_raw="Interest",
                column_sign=+1,
                section=INTEREST,
            ))
        return txns

    def _header(self, lines, pattern):
        matcher = re.compile(pattern, re.IGNORECASE)
        for line in lines:
            if matcher.search(line.text):
                return line
        raise ParseError(f"no table header matching {pattern!r}")

    def _date(self, text, line, period_start, period_end):
        try:
            return resolve_period_date(text, period_start, period_end)
        except DateParseError as exc:
            raise ParseError(str(exc), context={
                "page": line.page_number, "y": round(line.top, 1),
                "failed_on": "posting date", "raw_line": line.text,
            }) from exc


def _to_minor(text: str, line) -> int:
    try:
        minor, _ = parse_amount(text.strip(), default_currency=BASE_CURRENCY)
    except AmountParseError as exc:
        raise ParseError(f"cannot read {text!r} as an amount", context={
            "page": line.page_number, "y": round(line.top, 1), "raw_line": line.text,
        }) from exc
    return minor
