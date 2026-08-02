"""Shared parsing for Trust Bank statements.

Both Trust layouts — savings and credit card — print the same transaction
table: "Posting date | Description | Amount in FCY | Amount in SGD", with
amounts right-aligned and credits marked by a leading "+". This module holds
everything the two adapters have in common; the differences (account
identity, section structure, and which line marks the closing balance) live in
acc.py and cc.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ...domain.dates import DateParseError, parse_full_date, parse_period, resolve_period_date
from ...domain.models import ParsedTxn
from ...domain.money import AmountParseError, is_signed, parse_amount
from ...ports.parser import ParseError
from ..columns import ColumnBands
from ..pdfio import Document, Line

INSTITUTION = "Trust Bank"
BASE_CURRENCY = "SGD"

DATE_COL = "date"
DESC_COL = "description"
FCY_COL = "fcy"
SGD_COL = "sgd"

#: Page furniture rather than table rows. These repeat on every page and,
#: because their words fall wherever the layout puts them, several land in the
#: amount column and would otherwise be read as transactions.
_SKIP = re.compile(
    r"^\s*(?:Page\s+\d+\s+of\s+\d+\s*"
    r"|Trust\s+Bank\s+Singapore\s+Limited\s*"
    r"|GST\s+Reg\s+No\b.*"
    r"|TRANSACTION\s+DETAILS\s*)$",
    re.IGNORECASE,
)
_HEADER = re.compile(r"Posting\s+date.*Description", re.IGNORECASE)
_DAY_MONTH = re.compile(r"^\s*\d{1,2}\s+[A-Za-z]{3,9}\s*$")
#: "1 USD = 1.3394 SGD"
_FX_RATE = re.compile(r"^\s*1\s+([A-Z]{3})\s*=\s*([\d,]+\.?\d*)\s+([A-Z]{3})\s*$")

OPENING_LABEL = "previous balance"


@dataclass(frozen=True, slots=True)
class Row:
    """One assembled table row, before it is interpreted."""

    line: Line
    date_text: str
    description: str
    fcy_text: str
    sgd_text: str
    fx_rate: Decimal | None = None
    fx_rate_currency: str | None = None

    @property
    def label(self) -> str:
        return self.description.strip().lower()


def header_bands(line: Line) -> ColumnBands:
    """Derive the four column bands from the table's own header row."""
    words = list(line.words)
    description = next((w for w in words if w.text.lower().startswith("description")), None)
    amounts = [w for w in words if w.text.lower().startswith("amount")]
    if description is None or len(amounts) < 2:
        raise ParseError(f"unrecognised transaction table header: {line.text!r}")
    return ColumnBands.from_starts([
        (DATE_COL, 0.0),
        (DESC_COL, description.x0),
        (FCY_COL, amounts[0].x0),
        (SGD_COL, amounts[1].x0),
    ])


def find_header(document: Document) -> Line:
    for line in document.lines():
        if _HEADER.search(line.text):
            return line
    raise ParseError("no transaction table header found")


def is_skippable(line: Line) -> bool:
    return not line.text.strip() or bool(_SKIP.match(line.text))


def assemble_rows(lines: list[Line], bands: ColumnBands) -> list[Row]:
    """Turn visual lines into table rows, rejoining rows that span several.

    Foreign-currency transactions are printed across three lines: the merchant
    on one, the date and amounts on the next, and the exchange rate on a third.
    A description carried above its own row is held and attached to the dated
    row that follows it.
    """
    rows: list[Row] = []
    pending: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if is_skippable(line) or _HEADER.search(line.text):
            continue

        cells = bands.cells(line)
        date_text = cells[DATE_COL]
        description = cells[DESC_COL]
        fcy_text = cells[FCY_COL]
        sgd_text = cells[SGD_COL]

        # An exchange-rate line belongs to the row above it, not to itself.
        match = _FX_RATE.match(line.text.strip())
        if match:
            if rows:
                rows[-1] = _with_rate(rows[-1], match)
            continue

        # No amount means no transaction. This is what keeps section headings
        # ("Main Account"), page furniture and stray text out of the ledger,
        # and it is the single check that makes the row model safe: anything
        # the adapter cannot price is not a row.
        if not sgd_text:
            # A dateless description above an amountless line belongs to the
            # row that follows it — how foreign-currency rows are printed.
            if description and not date_text:
                pending.append(description)
            continue

        if pending:
            description = " ".join([*pending, description]).strip()
            pending = []

        rows.append(Row(line, date_text, description, fcy_text, sgd_text))
    return rows


def _with_rate(row: Row, match: re.Match) -> Row:
    return Row(
        line=row.line,
        date_text=row.date_text,
        description=row.description,
        fcy_text=row.fcy_text,
        sgd_text=row.sgd_text,
        fx_rate=Decimal(match.group(2).replace(",", "")),
        fx_rate_currency=match.group(1),
    )


def is_section_title(line: Line, bands: ColumnBands) -> bool:
    """A savings-pocket heading: text in the left column that is not a date."""
    cells = bands.cells(line)
    if cells[SGD_COL] or cells[FCY_COL] or cells[DESC_COL]:
        return False
    text = cells[DATE_COL].strip()
    return bool(text) and not _DAY_MONTH.match(text)


def signed_amount(text: str) -> int:
    """Read a Trust amount into signed minor units.

    Trust marks credits with a leading "+" and leaves debits bare, so the
    written sign — not the parsed magnitude — carries the direction. The result
    follows the project-wide convention: money into the account is positive.
    """
    try:
        minor, _ = parse_amount(text, default_currency=BASE_CURRENCY)
    except AmountParseError as exc:
        raise ParseError(f"cannot read {text!r} as an amount") from exc
    magnitude = abs(minor)
    return magnitude if is_signed(text) and not text.strip().startswith("-") else -magnitude


def balance_amount(text: str, *, owed_is_negative: bool) -> int:
    """Read a stated balance into the account-natural sign convention.

    For a deposit account the printed balance is the balance. For a card, the
    printed figure is what is *owed*, which is negative in this convention —
    unless the statement marks it "+", which uses the same "money in your
    favour" meaning it has in the transaction column.
    """
    try:
        minor, _ = parse_amount(text, default_currency=BASE_CURRENCY)
    except AmountParseError as exc:
        raise ParseError(f"cannot read {text!r} as a balance") from exc
    if not owed_is_negative:
        return minor
    magnitude = abs(minor)
    return magnitude if is_signed(text) and not text.strip().startswith("-") else -magnitude


def row_context(row: Row) -> dict:
    """Everything needed to debug a bad row without seeing the document."""
    return {
        "page": row.line.page_number,
        "y": round(row.line.top, 1),
        "raw_line": row.line.text,
        "columns": {
            "date": row.date_text,
            "description": row.description,
            "fcy": row.fcy_text,
            "sgd": row.sgd_text,
        },
    }


def build_txn(row: Row, period_start: date, period_end: date) -> ParsedTxn:
    context = row_context(row)
    try:
        posted = resolve_period_date(row.date_text, period_start, period_end)
    except DateParseError as exc:
        raise ParseError(str(exc), context={**context, "failed_on": "posting date"}) from exc

    fx_amount_minor = fx_currency = None
    if row.fcy_text:
        try:
            fx_amount_minor, fx_currency = parse_amount(row.fcy_text)
        except AmountParseError as exc:
            raise ParseError(
                f"cannot read foreign amount {row.fcy_text!r}",
                context={**context, "failed_on": "foreign currency amount"},
            ) from exc
        fx_amount_minor = abs(fx_amount_minor)

    try:
        amount_minor = signed_amount(row.sgd_text)
    except ParseError as exc:
        raise exc.with_context(**context, failed_on="settled amount") from exc

    return ParsedTxn(
        posted_date=posted,
        amount_minor=amount_minor,
        currency=BASE_CURRENCY,
        description_raw=row.description,
        fx_amount_minor=fx_amount_minor,
        fx_currency=fx_currency or row.fx_rate_currency,
        fx_rate=row.fx_rate,
    )


def find_period(document: Document, label: str) -> tuple[date, date]:
    """Read "Statement period 1 Jul 2025 - 31 Jul 2025" (or "Statement cycle")."""
    for line in document.lines():
        if label.lower() in line.text.lower():
            tail = re.split(label, line.text, flags=re.IGNORECASE)[-1]
            try:
                return parse_period(tail)
            except DateParseError:
                continue
    raise ParseError(f"no {label!r} found")


def find_statement_date(document: Document) -> date | None:
    for line in document.lines():
        if "statement date" in line.text.lower():
            tail = re.split("statement date", line.text, flags=re.IGNORECASE)[-1]
            try:
                return parse_full_date(tail)
            except DateParseError:
                return None
    return None


def transaction_lines(document: Document) -> list[Line]:
    """Every line from the TRANSACTION DETAILS heading to the end of the document."""
    collected: list[Line] = []
    started = False
    for line in document.lines():
        if not started:
            if "transaction details" in line.text.lower():
                started = True
            continue
        collected.append(line)
    if not started:
        raise ParseError("no TRANSACTION DETAILS section found")
    return collected
