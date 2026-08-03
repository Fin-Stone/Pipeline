"""Trust Bank savings account statement adapter.

One document carries one or more savings pockets — "Main Account", plus
whatever the customer has created. Each pocket has its own Previous balance
and Closing balance rows and becomes its own account, so each reconciles
independently.

The page-1 ACTIVITY SUMMARY is deliberately *not* used as the source of
balances. In the July 2025 sample its interest column is rendered against the
wrong pocket row, so trusting it would import a reconciliation failure. The
TRANSACTION DETAILS section is authoritative and self-consistent.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.models import DEPOSIT, DOC_TYPE_ACCOUNT, ParsedAccount, ParsedDocument
from ...ports.parser import ParseError
from .. import pdfio
from ..fingerprint import LayoutSignature
from . import base

#: What identifies a Trust savings statement.
#:
#: Both lines are Trust's own words — the bank naming itself and the statement
#: naming itself. Deliberately nothing customer-specific: the name and address
#: also appear in the header band, and routing on them would mean a change of
#: address broke the adapter. Verified against the June 2024 and July 2025
#: statements, which differ in pocket count, and the July 2026 one, which has a
#: different address and a newer Chromium.
SIGNATURE = LayoutSignature(
    producer=("skia", "pdf"),
    requires=(
        "trust bank singapore limited",
        "your savings account by trust statement is ready",
    ),
    page_size=(595, 842),
)

_ACCOUNT_REF = re.compile(r"\b(\d{2}-\d{6,8}-\d)\b")
_CLOSING_LABEL = "closing balance"


class TrustAccountAdapter:
    name = "trust.acc"
    version = "1.0.0"
    institution = base.INSTITUTION
    doc_type = DOC_TYPE_ACCOUNT

    def parse(self, path: Path, *, password: str | None = None) -> ParsedDocument:
        document = pdfio.load(Path(path), password=password)

        account_ref = self._account_ref(document)
        period_start, period_end = base.find_period(document, "Statement period")
        statement_date = base.find_statement_date(document)

        header = base.find_header(document)
        bands = base.header_bands(header)
        accounts = self._parse_pockets(document, bands, period_start, period_end, account_ref)

        if not accounts:
            raise ParseError("no savings pockets found in TRANSACTION DETAILS")

        return ParsedDocument(
            institution=self.institution,
            doc_type=self.doc_type,
            period_start=period_start,
            period_end=period_end,
            statement_date=statement_date,
            parser_version=f"{self.name}@{self.version}",
            accounts=tuple(accounts),
        )

    def _account_ref(self, document: pdfio.Document) -> str:
        for line in document.lines():
            if "account statement" in line.text.lower():
                match = _ACCOUNT_REF.search(line.text)
                if match:
                    return match.group(1)
        # Never guess an account: attaching transactions to the wrong one is
        # indistinguishable from correct behaviour until it is very expensive
        # to unwind.
        raise ParseError("no account reference found on the statement header")

    def _parse_pockets(self, document, bands, period_start, period_end, account_ref) -> list[ParsedAccount]:
        accounts: list[ParsedAccount] = []
        label: str | None = None
        section: list[pdfio.Line] = []

        for line in base.transaction_lines(document):
            if base.is_skippable(line):
                continue
            if base.is_section_title(line, bands):
                if label is not None:
                    accounts.append(self._build(label, section, bands, period_start, period_end, account_ref))
                label = line.text.strip()
                section = []
                continue
            section.append(line)

        if label is not None:
            accounts.append(self._build(label, section, bands, period_start, period_end, account_ref))
        return accounts

    def _build(self, label, lines, bands, period_start, period_end, account_ref) -> ParsedAccount:
        opening = closing = None
        txns = []
        for row in base.assemble_rows(lines, bands):
            if row.label == base.OPENING_LABEL:
                opening = base.balance_amount(row.sgd_text, owed_is_negative=False)
            elif row.label == _CLOSING_LABEL:
                closing = base.balance_amount(row.sgd_text, owed_is_negative=False)
            else:
                txns.append(base.build_txn(row, period_start, period_end))

        return ParsedAccount(
            account_ref_masked=account_ref,
            sub_account_label=label,
            currency=base.BASE_CURRENCY,
            kind=DEPOSIT,
            txns=tuple(txns),
            opening_balance_minor=opening,
            closing_balance_minor=closing,
        )
