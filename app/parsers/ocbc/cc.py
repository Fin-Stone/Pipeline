"""OCBC credit card statement adapter.

The simplest layout in the corpus: one line per transaction, one amount column,
and a balance pair printed either side of the rows. What it does have that the
others do not:

- **`CR` marks the credits.** Everything else is a purchase. An amount column
  with no sign at all means the direction is in the suffix, and a payment read
  as a purchase is wrong by twice its value.
- **Dates carry no year.** "20/01" is resolved against the statement period,
  the same way Trust's "01 Jun" is.
- **A card section per card, each with its own SUBTOTAL.** The sample holds one
  card, but the layout clearly allows several — TOTAL exists precisely to add
  the subtotals up — so sections are parsed rather than assumed to be single.
- **Reversed marginal text.** The page furniture below the table renders
  mirrored ("detimiL", "noitaroproC"). It is cut off by section end rather than
  filtered, because it is not table content at all.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.dates import DateParseError, parse_numeric_date, resolve_near_period
from ...domain.models import CARD, DOC_TYPE_CARD, ParsedAccount, ParsedDocument, ParsedTxn
from ...domain.money import AmountParseError, parse_amount
from ...ports.parser import ParseError
from .. import pdfio, tables
from ..fingerprint import LayoutSignature

INSTITUTION = "OCBC"
BASE_CURRENCY = "SGD"

#: What identifies an OCBC card statement.
#:
#: Both lines are the bank's own words. The customer's name and the reference
#: number on page 1 are deliberately excluded: routing must not depend on who
#: holds the account or on a number that changes every month.
#: The credit-limit heading is what makes this a *card* statement rather than
#: any other OCBC document, so it carries the discrimination and is required
#: whole. `requires` matches complete header lines, not prefixes.
SIGNATURE = LayoutSignature(
    producer=("streamline", "pdfgen"),
    requires=(
        "ocbc bank",
        "statement date payment due date total credit limit "
        "total available credit limit total minimum due",
    ),
)

DATE_COL = "date"
DESC_COL = "description"
AMOUNT_COL = "amount"

#: An amount, with either marker OCBC uses for a credit: a CR suffix, or
#: accounting parentheses around the figure.
_AMOUNT_IN_CELL = re.compile(r"\(?[\d,]*\d\.\d{2}\)?(?:\s*CR)?", re.IGNORECASE)
_IS_CREDIT = re.compile(r"\bCR\b", re.IGNORECASE)

#: What a transaction row's date cell looks like: day and month, no year.
_ROW_DATE = re.compile(r"^\d{1,2}[/-]\d{1,2}$")

_HEADER = re.compile(r"\bTRANSACTION\s+DATE\b.*\bDESCRIPTION\b.*\bAMOUNT\b", re.IGNORECASE)
_STATEMENT_DATE = re.compile(r"^\s*(\d{2}-\d{2}-\d{4})\b")

#: The balance markers, printed either side of a card's rows.
_OPENING = re.compile(r"^(last month'?s balance|previous balance|balance b/?f)\b", re.IGNORECASE)
_SUBTOTAL = re.compile(r"^sub\s*-?\s*total\b", re.IGNORECASE)
_TOTAL = re.compile(r"^total\b(?!\s+amount\s+due)", re.IGNORECASE)

#: A card section opens with its product name; the holder and number follow.
#: Digits may be masked, and statements differ about how.
_CARD_NUMBER = re.compile(r"\b(?:[\dX*]{4}[- ]){3}[\dX*]{4}\b", re.IGNORECASE)

#: Where the table stops. Everything past it is prose and mirrored furniture.
_SECTION_END = re.compile(r"^\s*(NEWS & INFORMATION|IMPORTANT NOTICE)", re.IGNORECASE)

#: Rows that are structure rather than spending.
_SKIP = re.compile(
    r"^\s*(TOTAL\s+AMOUNT\s+DUE|GRAND\s+TOTAL|This page is intentionally left blank)",
    re.IGNORECASE,
)


class OcbcCardAdapter:
    name = "ocbc.cc"
    version = "1.0.0"
    institution = INSTITUTION
    doc_type = DOC_TYPE_CARD

    def parse(self, path: Path, *, password: str | None = None) -> ParsedDocument:
        document = pdfio.load(Path(path), password=password)

        statement_date = self._statement_date(document)
        period_end = statement_date
        nominal_start = _month_before(statement_date)

        header = self._header(document)
        spec = tables.TableSpec(
            columns=tables.columns_from_header(header, [
                (DATE_COL, "Transaction", tables.DATE, 0),
                (DESC_COL, "Description", tables.TEXT, 0),
                # Unsigned: a purchase is money out, and CR reverses it.
                (AMOUNT_COL, "Amount", tables.MONEY, -1),
            ]),
            money_pattern=_AMOUNT_IN_CELL,
        )

        accounts = self._parse_accounts(document, spec, nominal_start, period_end)
        if not accounts:
            raise ParseError("no card sections found in Transaction Details")

        return ParsedDocument(
            institution=self.institution,
            doc_type=self.doc_type,
            period_start=_period_start(accounts, nominal_start),
            period_end=period_end,
            statement_date=statement_date,
            parser_version=f"{self.name}@{self.version}",
            accounts=tuple(accounts),
        )

    def _statement_date(self, document):
        for line in document.lines():
            match = _STATEMENT_DATE.match(line.text)
            if match:
                return parse_numeric_date(match.group(1))
        raise ParseError("no statement date found")

    def _header(self, document):
        for line in document.lines():
            if _HEADER.search(line.text):
                return line
        raise ParseError("no transaction table header found")

    def _parse_accounts(self, document, spec, period_start, period_end):
        """One account per card section, keyed by the card product.

        A card's *number* is deliberately not the key: it changes on reissue
        while the account continues, which would fork one history in two.
        """
        sections: dict[str, dict] = {}
        current = None

        lines = self._table_lines(document)
        for index, line in enumerate(lines):
            text = line.text.strip()
            if _SKIP.match(text):
                continue

            product = self._product(line, lines[index + 1:index + 3])
            if product is not None:
                current = sections.setdefault(
                    product, {"opening": None, "closing": None, "rows": []}
                )
                continue
            if current is None:
                continue

            # The holder-and-number line printed under a card's product name.
            # Not a row, and carrying exactly the two things a description must
            # never hold — left in, it wrapped onto the first transaction.
            if _CARD_NUMBER.search(text) and not _AMOUNT_IN_CELL.search(text):
                continue

            if _OPENING.match(text):
                current["opening"] = -self._amount(line, spec, text)
            elif _SUBTOTAL.match(text) or _TOTAL.match(text):
                # Owed, so negated: a card reconciles on the same formula a
                # deposit account does. SUBTOTAL closes this card's section;
                # TOTAL closes the last one when only one card is present.
                if current["closing"] is None:
                    current["closing"] = -self._amount(line, spec, text)
            else:
                current["rows"].append(line)

        return [self._build(name, s, spec, period_start, period_end)
                for name, s in sections.items()]

    def _product(self, line, following) -> str | None:
        """A card section's opening line: its product name.

        Identified by what comes *after* it — the cardholder and card number,
        which OCBC always prints directly beneath a product. Recognising it by
        its own shape instead is not enough: a statement long enough to run
        past page one repeats the bank's address block inside the table, and
        "1800 363 3333" is as capitalised-and-numeric as a card name is. It
        became a card section with no balances, and the real card's balances
        then attached to the phone number instead.
        """
        text = line.text.strip()
        if not text or _CARD_NUMBER.search(text) or _AMOUNT_IN_CELL.search(text):
            return None
        if not re.match(r"^[A-Z0-9][A-Z0-9 &'/.-]{4,}$", text):
            return None
        if not any(_CARD_NUMBER.search(nxt.text) for nxt in following):
            return None
        return " ".join(text.split()).title()

    def _amount(self, line, spec, text) -> int:
        match = _AMOUNT_IN_CELL.search(text)
        if not match:
            raise ParseError(f"no amount on the balance row {text!r}", context={
                "page": line.page_number, "y": round(line.top, 1), "raw_line": line.text,
            })
        return _to_minor(match.group(0), line)

    def _build(self, product, section, spec, period_start, period_end) -> ParsedAccount:
        rows = tables.assemble_rows(section["rows"], spec)
        txns = [t for t in (self._txn(r, spec, period_start, period_end) for r in rows) if t]

        if section["opening"] is None or section["closing"] is None:
            raise ParseError(
                f"card section {product!r} is missing its previous or closing balance"
            )

        return ParsedAccount(
            account_ref_masked=product,
            currency=BASE_CURRENCY,
            kind=CARD,
            txns=tuple(txns),
            opening_balance_minor=section["opening"],
            closing_balance_minor=section["closing"],
        )

    def _txn(self, row, spec, period_start, period_end) -> ParsedTxn | None:
        amounts = tables.money_cells(row, spec)
        if not amounts:
            return None

        column, text = amounts[0]
        date_text = row.cell(DATE_COL)
        if not _ROW_DATE.match(date_text.strip()):
            # Not a transaction. A statement running past one page repeats the
            # bank's contact block inside the table, and "Phone Banking" landed
            # in the date column of a line that happened to carry a figure.
            # Anything genuinely dropped here still has to reconcile, and the
            # balance check is what says so.
            return None
        try:
            # Widened backwards: OCBC prints no period, so the month before the
            # statement date is only a guess at where the cycle opened. A
            # purchase a few days either side of that guess is ordinary, and
            # resolving against the guess alone rejected it. The window stays
            # far short of a year, so the year is still unambiguous.
            posted = resolve_near_period(date_text, period_start, period_end)
        except DateParseError as exc:
            raise ParseError(str(exc), context={
                "page": row.line.page_number, "y": round(row.line.top, 1),
                "failed_on": "transaction date", "raw_line": row.line.text,
                "columns": dict(row.cells),
            }) from exc

        minor = _to_minor(text, row.line)
        # Two ways OCBC marks money coming back, and both must be read. A CR
        # suffix says so outright. Parentheses say it the accountant's way, and
        # `minor` already carries that as a negative, so applying the column's
        # direction to it reverses the purchase rather than adding one. Taking
        # abs() here counted every refund as another purchase.
        amount = abs(minor) if _IS_CREDIT.search(text) else column.sign * minor

        return ParsedTxn(
            posted_date=posted,
            amount_minor=amount,
            currency=BASE_CURRENCY,
            description_raw=row.description(DESC_COL),
        )

    def _table_lines(self, document) -> list[pdfio.Line]:
        """Lines from the table header to where the table stops.

        Cut rather than filtered: past the end the page carries prose and
        mirror-rendered furniture, none of which is table content.
        """
        lines = []
        started = False
        for line in document.lines():
            if _HEADER.search(line.text):
                started = True
                continue
            if not started:
                continue
            if _SECTION_END.match(line.text):
                started = False
                continue
            lines.append(line)
        return lines


def _to_minor(text: str, line) -> int:
    """The amount inside a cell, which is not always the whole of it.

    OCBC prints its company name rotated down the right margin, and on a full
    page one of those words lands on a transaction's baseline and inside the
    amount column — mirrored, so the cell reads "53.13 detimiL". Taking the
    amount-shaped token rather than the cell is what dbs/acc.py does for the
    same reason, and it costs nothing where the cell is already clean.
    """
    found = _AMOUNT_IN_CELL.findall(text or "")
    cleaned = re.sub(r"\s*CR\s*$", "", (found[-1] if found else text).strip(), flags=re.IGNORECASE)
    try:
        minor, _ = parse_amount(cleaned, default_currency=BASE_CURRENCY)
    except AmountParseError as exc:
        raise ParseError(f"cannot read {text!r} as an amount", context={
            "page": line.page_number, "y": round(line.top, 1), "raw_line": line.text,
        }) from exc
    return minor


def _period_start(accounts, nominal_start):
    """Where the cycle began, taken from the document rather than assumed.

    OCBC prints only a statement date, so the month before it is a guess. The
    rows are evidence: if one posted earlier than the guess, the cycle plainly
    opened earlier. Assuming otherwise is what rejected six correctly-read DBS
    statements. The upper bound stays the statement date, which is the half of
    the period check that can still catch a misdated row.
    """
    dates = [t.posted_date for a in accounts for t in a.txns]
    return min([*dates, nominal_start]) if dates else nominal_start


def _month_before(day):
    """The same day one month earlier, clamped to a real date."""
    month = day.month - 1 or 12
    year = day.year - (1 if day.month == 1 else 0)
    for attempt in range(day.day, 0, -1):
        try:
            return day.replace(year=year, month=month, day=attempt)
        except ValueError:
            continue
    raise ParseError(f"cannot find the month before {day}")
