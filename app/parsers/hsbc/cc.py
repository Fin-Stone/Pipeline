"""HSBC credit card statement adapter.

The layout is straightforward. What is not is the document: HSBC's generator
draws every character as a one-bit bitmap and embeds no fonts, so there is no
text in the file at all. `app/parsers/glyphs.py` reconstructs it before this
adapter sees anything, which is why nothing below has to know.

Three things this layout does that the others do not:

- **Two columns.** The transaction table sits on the left of the page and an
  account summary on the right, both carrying money. A line of text crosses
  both, so every read here is bounded by x — the summary's "Credit Limit
  5'000.00" is otherwise as much a transaction as anything in the table.
- **An apostrophe for thousands.** `4'000.00`, not `4,285.70`. Swiss notation,
  and stripped before parsing rather than taught to the money parser, because
  it is a property of this issuer's rendering and not of money.
- **Two date columns.** Posting date and transaction date, neither carrying a
  year. The statement prints its period in full — "From 22 JAN 2026 to 22 FEB
  2026" — so the year is resolved against a stated period rather than a guess,
  which is a better position than OCBC's.

The transaction date is what the row is filed under: it is when the money was
spent, and the posting date is the bank's own scheduling.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.dates import DateParseError, parse_full_date, resolve_near_period
from ...domain.models import CARD, DOC_TYPE_CARD, ParsedAccount, ParsedDocument, ParsedTxn
from ...domain.money import AmountParseError, parse_amount
from ...ports.parser import ParseError
from .. import cards, pdfio
from ..fingerprint import LayoutSignature

INSTITUTION = "HSBC"
BASE_CURRENCY = "SGD"

#: What identifies an HSBC card statement.
#:
#: Both lines are the bank's own furniture. The customer's name is on page one
#: and is deliberately not used: layout identity must not depend on who holds
#: the account — see `LayoutSignature`.
#:
#: The producer token is the renderer, which is also what tells `pdfio` the
#: text will have to be reconstructed from bitmaps.
SIGNATURE = LayoutSignature(
    producer=("opentext",),
    requires=(
        "post tran account summary sgd",
        "statement period total due minimum payment payment due date",
    ),
)

#: Where the transaction table ends and the account summary begins. Measured:
#: the table's amount column ends at x=376 and the summary starts at x=388.
COLUMN_SPLIT = 380.0

#: Column bands inside the transaction table.
POST_DATE = (50.0, 100.0)
TRAN_DATE = (100.0, 138.0)
DESCRIPTION = (138.0, 270.0)
AMOUNT = (270.0, COLUMN_SPLIT)

#: Where the summary's own amounts are right-aligned.
SUMMARY_AMOUNT = 500.0

#: An amount as this issuer prints it: apostrophes for thousands, an optional
#: CR suffix for money coming back.
_AMOUNT = re.compile(r"[\d']*\d\.\d{2}(?:\s*CR)?", re.IGNORECASE)
#: A trailing CR, which is how this issuer marks money coming back.
#:
#: Anchored to the end rather than matched as a word. HSBC glues the marker to
#: the figure — `188.81cR` — so `\bCR\b` finds no word boundary before the C
#: and reports every credit as a purchase. That read the card payment as a
#: second purchase and flipped the closing balance; the balance check caught it
#: and nothing else would have.
_IS_CREDIT = re.compile(r"CR\s*$", re.IGNORECASE)

#: A transaction row's date cell: day and short month, no year.
_ROW_DATE = re.compile(r"^\d{1,2}\s*[A-Za-z]{3}$")

#: "From 22 JAN 2026 to 22 FEB 2026", which is where the year comes from.
_PERIOD = re.compile(
    r"From\s+(\d{1,2}\s+[A-Za-z]{3,}\s+\d{4})\s+to\s+(\d{1,2}\s+[A-Za-z]{3,}\s+\d{4})",
    re.IGNORECASE,
)

#: The card this statement is for. Masked on the statement itself; the full
#: number appears once, near the top.
_CARD_NUMBER = re.compile(r"\b(\d{4})\s?(\d{4})\s?(\d{4})\s?(\d{4})\b")

#: Balance labels in the summary column. Matched on the label alone because
#: HSBC sets the closing figure on its own baseline below its heading.
_OPENING = re.compile(r"^previous statement balance\b", re.IGNORECASE)
_CLOSING = re.compile(r"^total account balance\b", re.IGNORECASE)

#: Where the table stops on a page. Past it the page carries the customer's
#: address and two columns of prose, all of which crosses the description band.
_TABLE_END = re.compile(r"^\s*(continued on next page|Ms?|Mrs)", re.IGNORECASE)

#: How far under a row its wrapped description may sit. Rows are ~20pt apart
#: and a wrap lands ~12pt below its row.
CONTINUATION_GAP = 16.0

#: Rows in the table that are structure rather than spending.
_SKIP = re.compile(
    r"^\s*(previous statement balance|continued on next page|post\b|date\b)",
    re.IGNORECASE,
)


class HsbcCardAdapter:
    name = "hsbc.cc"
    version = "1.0.0"
    institution = INSTITUTION
    doc_type = DOC_TYPE_CARD

    def parse(self, path: Path, *, password: str | None = None) -> ParsedDocument:
        document = pdfio.load(Path(path), password=password)

        period_start, period_end = self._period(document)
        opening, closing = self._balances(document)
        txns = self._transactions(document, period_start, period_end)

        return ParsedDocument(
            institution=self.institution,
            doc_type=self.doc_type,
            period_start=period_start,
            period_end=period_end,
            statement_date=period_end,
            parser_version=f"{self.name}@{self.version}",
            accounts=(
                ParsedAccount(
                    account_ref_masked=self._card(document),
                    currency=BASE_CURRENCY,
                    kind=CARD,
                    txns=tuple(txns),
                    opening_balance_minor=opening,
                    closing_balance_minor=closing,
                    # The same number the reference is built from. Recorded
                    # separately all the same: the reference is this account's
                    # identity and will stay put across a reissue, while the
                    # numbers accumulate. See app/parsers/cards.py.
                    card_numbers=cards.find_all(document.lines()),
                ),
            ),
        )

    def _period(self, document):
        for line in document.lines():
            found = _PERIOD.search(line.text)
            if found:
                try:
                    return parse_full_date(found.group(1)), parse_full_date(found.group(2))
                except DateParseError as exc:
                    raise ParseError(f"cannot read the statement period {found.group(0)!r}") from exc
        raise ParseError("no statement period found")

    def _card(self, document) -> str:
        """The card number, masked to its last four.

        Kept as the account reference rather than a product name because HSBC
        prints no product on this layout. Masked here, not later: the full
        number has no business in the ledger.
        """
        for line in document.lines():
            found = _CARD_NUMBER.search(line.text)
            if found:
                return f"xxxx-xxxx-xxxx-{found.group(4)}"
        raise ParseError("no card number found")

    def _balances(self, document) -> tuple[int, int]:
        """Opening and closing, from the summary column.

        Both are stored negated, as every card balance is: what the statement
        prints is what is *owed*, and negating it lets one reconciliation
        formula cover cards and deposit accounts alike. A `CR` suffix means the
        account is in credit, which is the one case where the sign flips back.
        """
        summary = [
            (line, line.text_between(COLUMN_SPLIT, 10_000).strip())
            for line in document.lines()
        ]
        summary = [(line, text) for line, text in summary if text]

        opening = closing = None
        for index, (line, text) in enumerate(summary):
            if opening is None and _OPENING.match(text):
                opening = self._summary_amount(summary, index, line)
            elif closing is None and _CLOSING.match(text):
                closing = self._summary_amount(summary, index, line)

        if opening is None or closing is None:
            raise ParseError(
                "the account summary is missing its previous or closing balance"
            )
        return opening, closing

    def _summary_amount(self, summary, index, line) -> int:
        """The figure belonging to a summary label.

        On the same line where it fits, on the next one where it does not:
        HSBC sets "Total Account Balance" and its figure on separate baselines
        when the label is long enough to need the width.
        """
        for candidate, _ in ((summary[index]), ) + tuple(
            (row, text) for row, text in summary[index + 1:index + 3]
        ):
            text = candidate.text_between(SUMMARY_AMOUNT, 10_000).strip()
            found = _AMOUNT.search(text)
            if found:
                minor = _to_minor(found.group(0), candidate)
                # Owed is negative; a CR balance means the bank owes the
                # household and is the one case that stays positive.
                return minor if _IS_CREDIT.search(found.group(0)) else -minor
        raise ParseError(
            f"no amount for the summary line {line.text.strip()!r}",
            context={"page": line.page_number, "y": round(line.top, 1)},
        )

    def _transactions(self, document, period_start, period_end) -> list[ParsedTxn]:
        """Rows from the left column, with wrapped descriptions folded in.

        A row is a row because it has a date in the date column. A line without
        one is the tail of the description above it — HSBC wraps a long
        merchant onto the next line with nothing else on it.
        """
        txns: list[ParsedTxn] = []
        pending: list[str] = []
        #: Where the last row was, so a continuation can be required to sit
        #: just under it. Without that bound the address block and two pages of
        #: prose below the table all landed in the description band and were
        #: folded into the final transaction.
        anchor: tuple[int, float] | None = None

        for line in document.lines():
            left = line.text_between(0, COLUMN_SPLIT).strip()
            if not left or _SKIP.match(left):
                continue
            if _TABLE_END.match(left):
                self._close(txns, pending)
                anchor = None
                continue

            date_text = line.text_between(*TRAN_DATE).strip()
            if not _ROW_DATE.match(date_text):
                # A continuation only if something is open and the text sits in
                # the description band. Anything else on this side of the page
                # is prose, and there is a lot of it.
                tail = line.text_between(*DESCRIPTION).strip()
                near = (
                    anchor is not None
                    and line.page_number == anchor[0]
                    and 0 < line.top - anchor[1] <= CONTINUATION_GAP
                )
                if near and tail and self._only_description(line):
                    pending.append(tail)
                continue

            self._close(txns, pending)
            anchor = (line.page_number, line.top)

            amount_text = line.text_between(*AMOUNT).strip()
            found = _AMOUNT.search(amount_text)
            if not found:
                continue

            try:
                posted = resolve_near_period(date_text, period_start, period_end)
            except DateParseError:
                # A date column holding something that is not a date is not a
                # transaction. Safe to drop quietly only because the balance
                # check still has to pass — if a real row went with it, the
                # statement will not reconcile.
                continue

            minor = _to_minor(found.group(0), line)
            txns.append(ParsedTxn(
                posted_date=posted,
                # A purchase is money out; CR reverses it. Read the suffix or a
                # payment counts as another purchase and the month is wrong by
                # twice the bill.
                amount_minor=minor if _IS_CREDIT.search(found.group(0)) else -minor,
                currency=BASE_CURRENCY,
                description_raw=line.text_between(*DESCRIPTION).strip(),
            ))

        self._close(txns, pending)
        return txns

    @staticmethod
    def _only_description(line) -> bool:
        """Whether a line lies wholly inside the description band.

        This is what separates a wrapped merchant name from prose. Distance
        alone is not enough: the third page repeats a block of terms and
        conditions, and one of its lines sits 14 points under a transaction —
        as close as a genuine wrap. But a wrap has nothing in the date columns
        and nothing in the amount column, while a paragraph starts at the left
        margin and runs the width of the page.
        """
        return not (
            line.words_between(0, DESCRIPTION[0])
            or line.words_between(DESCRIPTION[1], COLUMN_SPLIT)
        )

    @staticmethod
    def _close(txns, pending) -> None:
        if txns and pending:
            txns[-1] = ParsedTxn(
                posted_date=txns[-1].posted_date,
                amount_minor=txns[-1].amount_minor,
                currency=txns[-1].currency,
                description_raw=" ".join([txns[-1].description_raw, *pending]).strip(),
            )
        pending.clear()


def _to_minor(text: str, line) -> int:
    """An amount, with this issuer's apostrophes taken out.

    HSBC writes 4'000.00. The apostrophe is a rendering convention of this
    statement, not a fact about money, so it is removed here rather than taught
    to the money parser where it would loosen what every other adapter accepts.
    """
    cleaned = re.sub(r"\s*CR\s*$", "", text.strip(), flags=re.IGNORECASE).replace("'", "")
    try:
        minor, _ = parse_amount(cleaned, default_currency=BASE_CURRENCY)
    except AmountParseError as exc:
        raise ParseError(f"cannot read {text!r} as an amount", context={
            "page": line.page_number, "y": round(line.top, 1), "raw_line": line.text,
        }) from exc
    return abs(minor)
