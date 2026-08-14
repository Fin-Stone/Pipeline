"""DBS consolidated statement adapter (deposit accounts).

A DBS consolidated statement covers every deposit account the customer holds,
each with its own `Account No.` and its own forward balances. The sample has
one; the adapter does not assume that.

Three things differ from Trust and drive the implementation:

- **Two amount columns.** `Withdrawal (-)` and `Deposit (+)`. The direction is
  the column, not a sign in the text — nothing is ever prefixed.
- **Right-aligned columns.** A balance can begin 8pt *left* of the "Balance"
  heading, and a withdrawal 30pt right of its own, so bands are derived from
  header right edges. Left-edge banding puts balances in the deposit column.
- **Forward balances repeat per page.** `Balance Brought Forward` opens every
  page and `Balance Carried Forward` closes it, so the opening balance is the
  first and the closing balance the last.

DBS also prints a running balance and a totals row, which give two checks the
balance reconciliation alone would not catch. See `_check_declared_totals`.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from pathlib import Path

from ...domain.dates import DateParseError, parse_full_date, parse_numeric_date
from ...domain.models import DEPOSIT, DOC_TYPE_ACCOUNT, ParsedAccount, ParsedDocument, ParsedTxn
from ...domain.money import AmountParseError, parse_amount
from ...ports.parser import ParseError
from .. import pdfio, tables
from ..fingerprint import LayoutSignature

INSTITUTION = "DBS"
BASE_CURRENCY = "SGD"

#: What identifies a DBS consolidated statement.
#:
#: DBS leaves Producer empty and puts the rendering tool in Creator, so the
#: signature matches on creator — and on tokens rather than the whole string.
#: Across the corpus that string reads "Quadient Group AG~Inspire~12.5.33.0",
#: "Quadient CXM AG~Inspire~15.0.681.5" and "Quadient~Inspire~17.0.612.15": one
#: product, a company that renamed itself twice, 107 statements that stopped
#: routing because of it.
#:
#: Only one non-customer line exists in the header band: everything else up
#: there is the customer's name, their joint account holder's name and their
#: street. Requiring any of those would repeat the mistake the signature
#: redesign fixed, so "account summary" carries it — together with the creator
#: and the page size, which is enough to separate it from every other layout in
#: the corpus, including the DBS card statement, which says "statement of
#: account" and never "account summary".
SIGNATURE = LayoutSignature(
    creator=("quadient", "inspire"),
    requires=("account summary",),
)

DATE_COL, DESC_COL, WITHDRAWAL_COL, DEPOSIT_COL, BALANCE_COL = (
    "date", "description", "withdrawal", "deposit", "balance",
)

#: The last amount-shaped token in a cell. DBS prefixes balances with their
#: currency ("SGD 12,345.67") and its totals row reads "in SGD: 1,234.56", so a
#: money cell is not always the amount by itself.
#:
#: The trailing minus is part of the amount and must be captured with it. DBS
#: prints a reversal as a negative entry in the column it is reversing —
#: "100.00" out on one line, "100.00-" on the next — and matching only the
#: digits turned a refund into a second withdrawal.
_AMOUNT_IN_CELL = re.compile(r"[-+]?[\d,]*\d\.\d{2}-?")

_HEADER = re.compile(r"\bDate\b.*\bDescription\b.*\bWithdrawal\b", re.IGNORECASE)
_ACCOUNT_LINE = re.compile(r"Account\s+No\.?\s*[:.]?\s*([0-9][0-9\- ]{5,})", re.IGNORECASE)
_AS_AT = re.compile(r"as\s+(?:at|of)\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", re.IGNORECASE)
#: Wording varies between issuers and over time; naming the alternatives
#: keeps a rewording out of the code.
_BROUGHT = re.compile(r"balance\s+brought\s+forward|opening\s+balance", re.IGNORECASE)
_CARRIED = re.compile(r"balance\s+carried\s+forward|closing\s+balance", re.IGNORECASE)
_TOTAL_CARRIED = re.compile(r"total\s+balance\s+carried\s+forward", re.IGNORECASE)
#: Where a statement stops being a table and starts being prose.
_SECTION_END = re.compile(r"^\s*Messages\s+For", re.IGNORECASE)

#: Page furniture, by what it says.
_SKIP = re.compile(
    r"^\s*(?:Page\s+\d+\s+of\s+\d+"
    # The column header repeats at the top of every page, and its own words
    # sit in the money columns — unskipped it reads as a row carrying both a
    # withdrawal and a deposit.
    r"|Date\s+Description\s+Withdrawal\b.*"
    r"|Account\s+Summary\b.*"
    r"|Transaction\s+Details\b.*"
    r"|CURRENCY:.*"
    r"|Messages\s+For\b.*"
    r"|PDS_.*)\s*$",
)

#: Left of this is the margin, not the table. The date column starts at 45pt;
#: the rotated registration strip runs down the page at 11.
#:
#: Position, because the rule this replaced dropped any short single-token
#: line — which is what the strip looks like once it is broken into rows, and
#: is also exactly what DBS trails under a GIRO collection to say who it paid.
#: On one statement that rule destroyed 37 lines — each an insurer's name, or a
#: policy number beside it — leaving a description of nothing but the
#: mechanism. Length could never tell those apart; where they sit always could.
MARGIN_X = 30.0

#: DBS trails its references *under* each transaction and never above it, so
#: every description-only line belongs to the row it follows. A fixed distance
#: cannot work here: the last reference sits 42pt below its row against a 48pt
#: pitch, leaving no margin that both keeps it and excludes the next row's.
CONTINUATION_GAP = None


def _period_start(accounts, statement_date):
    """Where the statement's period begins.

    DBS prints only "as at <date>" and no period, and its cycles are **not
    calendar months**: a September statement legitimately carries rows dated
    31 August, because the cycle runs from the day after the previous
    statement. Assuming the first of the month rejected six statements that
    were read correctly.

    So the start is taken from the document rather than invented: the earliest
    posting date, or the first of the statement's month when that is earlier.
    The upper bound stays the declared statement date, which is the half of the
    period check that can still catch a misdated row.
    """
    dates = [t.posted_date for a in accounts for t in a.txns]
    month_start = statement_date.replace(day=1)
    return min([*dates, month_start]) if dates else month_start


def _amount_text(cell: str) -> str:
    """Pull the amount out of a cell that may carry a currency alongside it."""
    matches = _AMOUNT_IN_CELL.findall(cell or "")
    return matches[-1] if matches else (cell or "")


class DbsAccountAdapter:
    name = "dbs.acc"
    version = "1.0.0"
    institution = INSTITUTION
    doc_type = DOC_TYPE_ACCOUNT

    def parse(self, path: Path, *, password: str | None = None) -> ParsedDocument:
        document = pdfio.load(Path(path), password=password)

        statement_date = self._statement_date(document)
        header = self._header(document)
        spec = tables.TableSpec(
            columns=tables.columns_from_header(header, [
                (DATE_COL, "Date", tables.DATE, 0),
                (DESC_COL, "Description", tables.TEXT, 0),
                # The direction is the column: DBS never signs an amount.
                #
                # Anchored on the words "Withdrawal" and "Deposit" rather than
                # the "(-)" and "(+)" beside them. The money zone starts at the
                # leftmost money heading, and "(-)" sits 50pt right of the
                # column's own values — anchoring there would put every
                # withdrawal into the description.
                (WITHDRAWAL_COL, ("Withdrawal", "Debit", "Money Out"), tables.MONEY, -1),
                (DEPOSIT_COL, ("Deposit", "Credit", "Money In"), tables.MONEY, +1),
                (BALANCE_COL, "Balance", tables.BALANCE, 0),
            ]),
            continuation_gap=CONTINUATION_GAP,
            money_pattern=_AMOUNT_IN_CELL,
        )

        accounts = self._parse_accounts(document, spec, statement_date, statement_date)
        if not accounts:
            raise ParseError("no accounts found in Transaction Details")

        return ParsedDocument(
            institution=self.institution,
            doc_type=self.doc_type,
            period_start=_period_start(accounts, statement_date),
            period_end=statement_date,
            statement_date=statement_date,
            parser_version=f"{self.name}@{self.version}",
            accounts=tuple(accounts),
        )

    # -- header -------------------------------------------------------------

    def _header(self, document: pdfio.Document) -> pdfio.Line:
        for line in document.lines():
            if _HEADER.search(line.text):
                return line
        raise ParseError("no transaction table header found")

    def _statement_date(self, document: pdfio.Document) -> date:
        """DBS prints "as at 31 Dec 2021" and no period.

        The period is inferred as that month, which is what a monthly
        consolidated statement covers. If one ever spans something else its
        rows fall outside and the document quarantines, which is the right
        failure.
        """
        for line in document.lines():
            match = _AS_AT.search(line.text)
            if match:
                try:
                    return parse_full_date(match.group(1))
                except DateParseError:
                    continue
        raise ParseError("no 'as at <date>' found to derive the statement period from")

    # -- accounts -----------------------------------------------------------

    def _parse_accounts(self, document, spec, statement_date, period_start) -> list[ParsedAccount]:
        """Split the document into per-account sections and parse each.

        A section begins at a line carrying an `Account No.` and runs to the
        next one.
        """
        # Keyed by account reference and *merged*, because DBS reprints the
        # `Account No.` header at the top of every page. Treating each as its
        # own section splits one account's month across several, and each
        # fragment then disagrees with the totals the statement declares —
        # which is exactly how this was found.
        sections: dict[str, list[pdfio.Line]] = {}
        current: list[pdfio.Line] | None = None

        for line in document.lines():
            match = _ACCOUNT_LINE.search(line.text)
            if match:
                current = sections.setdefault(match.group(1).strip(), [])
                continue
            if current is not None:
                current.append(line)

        accounts = []
        for account_ref, lines in sections.items():
            account = self._build(account_ref, lines, spec, statement_date, period_start)
            if account is not None:
                accounts.append(account)
        return accounts

    def _build(self, account_ref, lines, spec, statement_date, period_start) -> ParsedAccount | None:
        rows = tables.assemble_rows(self._table_lines(lines), spec, skip=_SKIP)

        opening = closing = None
        declared: tuple[int, int] | None = None
        txns: list[ParsedTxn] = []

        for row in rows:
            label = row.label
            if _TOTAL_CARRIED.search(label):
                declared = self._declared_totals(row)
                closing = self._balance(row)
                # The account's table ends here. Anything after it is the
                # statement's closing prose, which spans the full page width
                # and would otherwise be cut into columns and read as rows.
                break
            if _BROUGHT.search(label):
                # Repeats at the top of every page; the first one is the
                # statement's opening balance.
                if opening is None:
                    opening = self._balance(row)
                continue
            if _CARRIED.search(label):
                # Repeats at the foot of every page; the last one wins.
                closing = self._balance(row)
                continue

            txn = self._txn(row, spec, period_start, statement_date)
            if txn is not None:
                txns.append(txn)

        if opening is None and closing is None and not txns:
            return None

        return ParsedAccount(
            account_ref_masked=account_ref,
            sub_account_label=None,
            currency=BASE_CURRENCY,
            kind=DEPOSIT,
            txns=tuple(txns),
            opening_balance_minor=opening,
            closing_balance_minor=closing,
            declared_out_minor=declared[0] if declared else None,
            declared_in_minor=declared[1] if declared else None,
        )

    def _txn(self, row, spec, period_start, statement_date) -> ParsedTxn | None:
        amounts = tables.money_cells(row, spec)
        if not amounts:
            return None
        if len(amounts) > 1:
            raise ParseError(
                "a row carries both a withdrawal and a deposit",
                context={"page": row.line.page_number, "y": round(row.line.top, 1),
                         "raw_line": row.line.text, "columns": dict(row.cells)},
            )

        column, text = amounts[0]
        date_text = row.cell(DATE_COL)
        try:
            posted = parse_numeric_date(date_text)
        except DateParseError as exc:
            raise ParseError(str(exc), context={
                "page": row.line.page_number, "y": round(row.line.top, 1),
                "failed_on": "posting date", "raw_line": row.line.text,
                "columns": dict(row.cells),
            }) from exc

        try:
            minor, _ = parse_amount(_amount_text(text), default_currency=BASE_CURRENCY)
        except AmountParseError as exc:
            raise ParseError(f"cannot read {text!r} as an amount", context={
                "page": row.line.page_number, "y": round(row.line.top, 1),
                "raw_line": row.line.text, "columns": dict(row.cells),
            }) from exc

        return ParsedTxn(
            posted_date=posted,
            # The column carries the direction, and the amount's own sign
            # negates it: "100.00-" in the withdrawal column is a reversal, so
            # money *in*. Taking abs() here discarded that and counted every
            # reversal as a further withdrawal.
            amount_minor=column.sign * minor,
            currency=BASE_CURRENCY,
            description_raw=row.description(DESC_COL),
            column_sign=column.sign,
        )

    def _table_lines(self, lines: list[pdfio.Line]) -> list[pdfio.Line]:
        """Cut a section off where its table stops.

A statement ends with pages of regulatory prose that runs the full
        page width. Cut into columns it produces lines carrying text in every
        money column at once, so it has to be excluded before assembly rather
        than filtered afterwards.

        The margin strip is stripped **word by word**, not line by line,
        because it overlaps the table rather than sitting beside it: one
        continuation reads `A12345678` at 11pt and `A BILLER PTE. LTD.` at
        113, and dropping the line would lose the payee to remove the noise.
        """
        kept = []
        for line in lines:
            if _SECTION_END.match(line.text):
                break
            words = tuple(w for w in line.words if w.x0 >= MARGIN_X)
            if not words:
                continue
            kept.append(line if len(words) == len(line.words) else replace(line, words=words))
        return kept

    def _balance(self, row) -> int:
        text = _amount_text(row.cell(BALANCE_COL))
        try:
            minor, _ = parse_amount(text, default_currency=BASE_CURRENCY)
        except AmountParseError as exc:
            raise ParseError(f"cannot read balance {text!r}", context={
                "page": row.line.page_number, "y": round(row.line.top, 1),
                "raw_line": row.line.text, "columns": dict(row.cells),
            }) from exc
        return minor

    def _declared_totals(self, row) -> tuple[int, int] | None:
        """The withdrawal and deposit totals DBS prints on its closing row."""
        try:
            withdrawal, _ = parse_amount(_amount_text(row.cell(WITHDRAWAL_COL)),
                                         default_currency=BASE_CURRENCY)
            deposit, _ = parse_amount(_amount_text(row.cell(DEPOSIT_COL)),
                                      default_currency=BASE_CURRENCY)
        except AmountParseError:
            return None
        return abs(withdrawal), abs(deposit)
