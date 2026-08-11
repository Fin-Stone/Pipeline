"""Shared parsing for Trust Bank statements.

Both Trust layouts — savings and credit card — print the same transaction
table: "Posting date | Description | Amount in FCY | Amount in SGD", with
amounts right-aligned and credits marked by a leading "+". This module holds
everything the two adapters have in common; the differences (account
identity, section structure, and which line marks the closing balance) live in
acc.py and cc.py.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ...domain.dates import (
    DateParseError,
    parse_full_date,
    parse_period,
    resolve_near_period,
    resolve_period_date,
)
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
#: A single "01 Jun" anywhere in the date column. Newer statements print two.
_DATE_TOKEN = re.compile(r"\d{1,2}\s+[A-Za-z]{3,9}")
#: "1 USD = 1.3394 SGD"
_FX_RATE = re.compile(r"^\s*1\s+([A-Z]{3})\s*=\s*([\d,]+\.?\d*)\s+([A-Z]{3})\s*$")

log = logging.getLogger("finstone.parsers.trust")

OPENING_LABEL = "previous balance"

#: A description-only line this far below the last line of a row belongs to
#: that row.
#:
#: Measured from the statements: a card statement's wrapped parts sit 6.000pt
#: apart, a savings statement's 9.005pt, the next row's lead-in about 13pt, and
#: the row pitch 24.75pt and up. Anything past this threshold is treated as a
#: lead-in for the row that follows, which is how foreign-currency rows print
#: their merchant.
#:
#: It was 9.0, which is *below* the 9.004565pt a savings statement actually
#: wraps at, so a merchant's address never attached to its own row at all — it
#: was held over and prepended to the next one instead. Set above the widest
#: wrap and well below the narrowest lead-in, so neither end is decided by a
#: rounding error.
CONTINUATION_GAP = 11.0


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

    The distance to a continuation is measured from the last line already
    attached to the row rather than from the row itself, because a merchant's
    address wraps onto two lines about 9pt apart: measuring from the row put the
    second line 18pt away, past the threshold, so it was held over and prepended
    to the next row instead — "SAMPLE CIRCLE #01-01 GROCER HUB SINGAPORE
    000000 ..ID:T00XX0000X Interest" was one month's interest credit. A held
    fragment is also required to sit just above the row that claims it, and on
    the same page, or anything left over attaches to whatever comes next.
    """
    rows: list[Row] = []
    #: Held fragments with where they were printed, so a row only claims one
    #: that is genuinely above it.
    pending: list[tuple[int, float, str]] = []
    #: Page and baseline of the last line belonging to the row being built.
    anchor: tuple[int, float] | None = None
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
            if description and not date_text:
                wraps_under = (
                    rows and anchor is not None
                    and line.page_number == anchor[0]
                    and 0 < line.top - anchor[1] <= CONTINUATION_GAP
                )
                if wraps_under:
                    # A long merchant name wrapping under its own row. Without
                    # this it would be held over and prepended to the *next*
                    # row's description, corrupting both.
                    rows[-1] = _with_extra_description(rows[-1], description)
                    # The wrap continues from here, so a second line is measured
                    # against this one and not against the row.
                    anchor = (line.page_number, line.top)
                else:
                    # Further away: a description printed above the row it
                    # belongs to, which is how foreign-currency rows print.
                    pending.append((line.page_number, line.top, description))
            continue

        lead_ins = [
            text for page, top, text in pending
            if page == line.page_number and 0 < line.top - top <= CONTINUATION_GAP
        ]
        if lead_ins:
            description = " ".join([*lead_ins, description]).strip()
        pending = []

        rows.append(Row(line, date_text, description, fcy_text, sgd_text))
        anchor = (line.page_number, line.top)
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


def _with_extra_description(row: Row, extra: str) -> Row:
    return Row(
        line=row.line,
        date_text=row.date_text,
        description=" ".join(part for part in (row.description, extra) if part).strip(),
        fcy_text=row.fcy_text,
        sgd_text=row.sgd_text,
        fx_rate=row.fx_rate,
        fx_rate_currency=row.fx_rate_currency,
    )


def is_section_title(line: Line, bands: ColumnBands) -> bool:
    """A savings-pocket heading: text in the left column holding no date.

    Tested by searching for a date rather than matching the whole cell, so a
    two-date row is never mistaken for a heading.
    """
    cells = bands.cells(line)
    if cells[SGD_COL] or cells[FCY_COL] or cells[DESC_COL]:
        return False
    text = cells[DATE_COL].strip()
    return bool(text) and not _DATE_TOKEN.search(text)


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
    posted, value = _resolve_dates(row, period_start, period_end, context)

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
        value_date=value,
        amount_minor=amount_minor,
        currency=BASE_CURRENCY,
        description_raw=row.description,
        fx_amount_minor=fx_amount_minor,
        fx_currency=fx_currency or row.fx_rate_currency,
        fx_rate=row.fx_rate,
    )


def _resolve_dates(row: Row, period_start: date, period_end: date, context: dict) -> tuple[date, date | None]:
    """Read the date column, which holds one date or two.

    Statements up to 2023 printed a single "Posting date". Newer ones print
    **transaction date then posting date** — the purchase happened on the
    first, it hit the account on the second. Only the second is inside the
    statement period, and only the second is what reconciliation and the
    period check depend on, so the *last* date is always the posting date.

    That ordering is Trust's format encoded as adapter knowledge. If it were
    ever wrong, the posting date would fall outside the period and the
    document would quarantine loudly rather than importing something subtly
    misdated.
    """
    found = _DATE_TOKEN.findall(row.date_text or "")

    if not found:
        raise ParseError(
            f"no date found in {row.date_text!r}",
            context={**context, "failed_on": "posting date"},
        )

    try:
        posted = resolve_period_date(found[-1], period_start, period_end)
    except DateParseError as exc:
        raise ParseError(str(exc), context={**context, "failed_on": "posting date"}) from exc

    if len(found) < 2:
        return posted, None

    # The transaction date routinely precedes the period — a purchase on
    # 29 Dec posting on 2 Jan is normal. Nothing in this phase reads
    # value_date (not reconciliation, not the period check, not the dedupe
    # key), so a date that will not resolve is recorded as absent rather than
    # rejecting a document that otherwise reconciles to the cent.
    try:
        value = resolve_near_period(found[0], period_start, period_end)
    except DateParseError as exc:
        log.warning(
            "could not resolve transaction date %r on %s: %s", found[0], row.line.location, exc
        )
        return posted, None

    return posted, value


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
