"""MariBank credit card statement adapter.

Two things make this layout unlike the others in the corpus.

**A row is printed as three lines, and the middle one carries the values.** The
description occupies a two-line cell and the dates and amount are centred
against it, so the merchant is printed *above* the dated line and the
transaction type below:

    zhangjihui002.sg          <- merchant
    19 AUG  08 AUG   -45.34   <- the row proper
    Instant Checkout          <- type

The shared engine already rejoins fragments either side of a row; this is the
layout that needed the "above" case to respect distance, so that a category
heading sitting over the first row of its section is not read as part of it.

**MariBank publishes no PDF metadata at all** — producer and creator are both
empty — so routing rests entirely on the header lines. That is no weaker here
than elsewhere: the vendor string was never the discriminating part, and
`docs/ingestion.md` records why signatures stopped depending on it.

Amounts carry their own sign, so nothing has to be inferred from a column.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.dates import (
    DateParseError,
    parse_full_date,
    resolve_near_period,
    resolve_period_date,
)
from ...domain.models import CARD, DOC_TYPE_CARD, ParsedAccount, ParsedDocument, ParsedTxn
from ...domain.money import AmountParseError, parse_amount
from ...ports.parser import ParseError
from .. import cards, pdfio, tables
from ..fingerprint import LayoutSignature

INSTITUTION = "MariBank"
BASE_CURRENCY = "SGD"

#: What identifies a MariBank card statement.
#:
#: No producer or creator: MariBank ships neither. Both required lines are the
#: bank's own words, and the strapline is what separates a card statement from
#: the savings one.
SIGNATURE = LayoutSignature(
    requires=(
        "mari credit card statement",
        "account statement date credit limit statement due minimum payment",
    ),
)

#: The card product, which is the account's stable identity rather than the
#: card number. See trust/cc.py for why a number cannot be the key.
PRODUCT = "Mari Credit Card"

POSTED_COL = "posted"
TXN_DATE_COL = "transaction"
DESC_COL = "description"
AMOUNT_COL = "amount"

#: Two lines of description, so the dated line sits about 7pt from each. Well
#: clear of the ~25pt that separates one row from the next.
CONTINUATION_GAP = 12.0

_AMOUNT_IN_CELL = re.compile(r"-?[\d,]*\d\.\d{2}")
_HEADER = re.compile(r"\bPOSTED\s+DATE\b.*\bDESCRIPTION\b.*\bAMOUNT\b", re.IGNORECASE)
_PERIOD = re.compile(
    r"STATEMENT\s+PERIOD:\s*(.+?)\s+to\s+(.+?)\s*$", re.IGNORECASE
)
_PREVIOUS = re.compile(r"^\s*PREVIOUS\s+OUTSTANDING\b", re.IGNORECASE)
_CURRENT = re.compile(r"^\s*CURRENT\s+OUTSTANDING\b", re.IGNORECASE)
#: The lettered column markers under the statement summary headings. The row of
#: figures directly beneath them is the summary, and (A) is the first of them.
_SUMMARY_MARKERS = re.compile(r"^\s*\(A\)\s*\(B\)\s*\(C\)", re.IGNORECASE)

#: Where the transaction table stops and the statement's small print begins.
_SECTION_END = re.compile(
    r"^\s*(Important Information|WARNING:|Minimum Payment Warning|For example,)",
    re.IGNORECASE,
)


class MariBankCardAdapter:
    name = "maribank.cc"
    version = "1.0.0"
    institution = INSTITUTION
    doc_type = DOC_TYPE_CARD

    def parse(self, path: Path, *, password: str | None = None) -> ParsedDocument:
        document = pdfio.load(Path(path), password=password)

        period_start, period_end = self._period(document)
        opening, closing = self._balances(document)

        header = self._header(document)
        spec = tables.TableSpec(
            columns=tables.columns_from_header(header, [
                (POSTED_COL, "Posted", tables.DATE, 0),
                (TXN_DATE_COL, "Transaction", tables.DATE, 0),
                (DESC_COL, "Description", tables.TEXT, 0),
                # Sign 0: the amount is written signed, so nothing is inferred
                # from which column it landed in.
                (AMOUNT_COL, "Amount", tables.MONEY, 0),
            ]),
            continuation_gap=CONTINUATION_GAP,
            money_pattern=_AMOUNT_IN_CELL,
        )

        txns = []
        for section, lines in self._sections(document, spec):
            for row in tables.assemble_rows(lines, spec):
                txn = self._txn(row, period_start, period_end, section)
                if txn is not None:
                    txns.append(txn)

        account = ParsedAccount(
            account_ref_masked=PRODUCT,
            currency=BASE_CURRENCY,
            kind=CARD,
            txns=tuple(txns),
            opening_balance_minor=opening,
            closing_balance_minor=closing,
            # MariBank prints only the tail, behind a run of asterisks. That is
            # all the matching needs — see app/parsers/cards.py.
            card_numbers=cards.find_all(document.lines()),
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

    def _balances(self, document):
        """Opening and closing, from the statement summary rather than the table.

        MariBank prints no running total under the transactions; the summary
        block on page 1 is the only place the two balances appear. They are
        amounts *owed*, so they are stored negated and the card reconciles on
        the same formula a deposit account does.
        """
        lines = list(document.lines())
        opening = closing = None

        for index, line in enumerate(lines):
            if _CURRENT.match(line.text):
                closing = -self._last_amount(line)
            elif _SUMMARY_MARKERS.match(line.text) and index + 1 < len(lines):
                # (A) is previous outstanding, and it leads the row of figures.
                amounts = _AMOUNT_IN_CELL.findall(lines[index + 1].text)
                if amounts:
                    opening = -_to_minor(amounts[0], lines[index + 1])
            elif opening is None and _PREVIOUS.match(line.text):
                opening = -self._last_amount(line)

        if opening is None or closing is None:
            raise ParseError(
                "card statement is missing its previous or current outstanding balance"
            )
        return opening, closing

    def _last_amount(self, line) -> int:
        amounts = _AMOUNT_IN_CELL.findall(line.text)
        if not amounts:
            raise ParseError(f"no amount on the balance row {line.text!r}", context={
                "page": line.page_number, "y": round(line.top, 1), "raw_line": line.text,
            })
        return _to_minor(amounts[-1], line)

    def _header(self, document):
        for line in document.lines():
            if _HEADER.search(line.text):
                return line
        raise ParseError("no transaction table header found")

    def _sections(self, document, spec) -> list[tuple[str | None, list]]:
        """The table split at its category headings.

        A heading is found by where it sits, not by what it says. MariBank
        centres its headings over the description column, so they overhang its
        left edge, while every description and type line is flush against it.
        That matters because the two are not distinguishable by wording: a
        repayment row is filed under "Repayment/Conversion" and carries the
        type "Repayment" on the line directly below it. Matching on words alone
        read that type line as a new section and swallowed it out of the
        description.

        Grouping is not cosmetic either. Each heading's rows are ordered on
        their own, so the ordering check has to see them apart, and splitting
        here stops a description wrapping across a heading into the wrong row.
        """
        description = next(c for c in spec.columns if c.name == DESC_COL)
        groups: list[tuple[str | None, list]] = [(None, [])]

        for line in self._table_lines(document):
            if self._is_heading(line, description.left):
                groups.append((" ".join(line.text.split()), []))
                continue
            groups[-1][1].append(line)
        return [g for g in groups if g[1]]

    def _is_heading(self, line, description_left: float) -> bool:
        """Centred over the description column, and carrying no value.

        The amount test is what separates a heading from an ordinary row,
        whose date columns also start well left of the description.
        """
        if not line.words or _AMOUNT_IN_CELL.search(line.text):
            return False
        return line.words[0].x0 < description_left

    def _txn(self, row, period_start, period_end, section) -> ParsedTxn | None:
        posted_text = row.cell(POSTED_COL)
        if not posted_text.strip():
            # Small print carrying a figure, not a transaction. A row without a
            # posting date is not one.
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
        txn_text = row.cell(TXN_DATE_COL)
        if txn_text.strip():
            try:
                # The purchase routinely predates the cycle it posts in.
                value_date = resolve_near_period(txn_text, period_start, period_end)
            except DateParseError as exc:
                raise ParseError(str(exc), context={
                    "page": row.line.page_number, "y": round(row.line.top, 1),
                    "failed_on": "transaction date", "raw_line": row.line.text,
                    "columns": dict(row.cells),
                }) from exc

        return ParsedTxn(
            posted_date=posted,
            value_date=value_date,
            # Already signed on the page: a purchase is negative, a repayment
            # positive, which is the convention the ledger uses unchanged.
            amount_minor=_to_minor(row.cell(AMOUNT_COL), row.line),
            currency=BASE_CURRENCY,
            description_raw=row.description(DESC_COL),
            section=section,
        )

    def _table_lines(self, document) -> list[pdfio.Line]:
        """Lines between the table header and the small print.

        Cut rather than filtered: the closing pages carry worked examples full
        of money-shaped figures, and reading those as rows is exactly the
        failure the section end exists to prevent.
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
    try:
        minor, _ = parse_amount(text.strip(), default_currency=BASE_CURRENCY)
    except AmountParseError as exc:
        raise ParseError(f"cannot read {text!r} as an amount", context={
            "page": line.page_number, "y": round(line.top, 1), "raw_line": line.text,
        }) from exc
    return minor
