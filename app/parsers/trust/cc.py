"""Trust Bank credit card statement adapter.

Two things differ from the savings layout:

- **A card account is identified by its product, not its number.** Card numbers
  change when a card is reissued or replaced while the account continues, so
  keying on the number would fork one account's history in two. The product —
  the card brand — is what actually distinguishes two cards held at the same
  bank, and it is stable for the life of the account. Trust issues a single
  card product and prints no card number anywhere in the statement (every page
  was checked), so the product name is asserted here as per-institution
  knowledge of the format.

- **The closing balance row reads "Total outstanding balance"**, and the
  figures are amounts *owed*. Following the project-wide sign convention they
  are stored negated, so a card statement reconciles with the same formula a
  savings statement does.
"""

from __future__ import annotations

from pathlib import Path

from ...domain.models import CARD, DOC_TYPE_CARD, ParsedAccount, ParsedDocument
from ...ports.parser import ParseError
from .. import pdfio
from ..fingerprint import LayoutSignature
from . import base

#: What identifies a Trust credit card statement.
#:
#: The strapline is what separates a card statement from a savings one, so it
#: carries the discrimination. Nothing customer-specific is required; see
#: acc.py for why.
SIGNATURE = LayoutSignature(
    producer=("skia", "pdf"),
    requires=(
        "trust bank singapore limited",
        "hello your trust credit card statement is ready",
    ),
)

#: The card product, which is the account's stable identity. See the module
#: docstring for why this is not a card number.
PRODUCT = "Trust Credit Card"

_CLOSING_LABELS = ("total outstanding balance", "current outstanding balance", "closing balance")


class TrustCardAdapter:
    name = "trust.cc"
    version = "1.0.0"
    institution = base.INSTITUTION
    doc_type = DOC_TYPE_CARD

    def parse(self, path: Path, *, password: str | None = None) -> ParsedDocument:
        document = pdfio.load(Path(path), password=password)

        period_start, period_end = base.find_period(document, "Statement cycle")
        statement_date = base.find_statement_date(document)

        header = base.find_header(document)
        bands = base.header_bands(header)

        opening = closing = None
        txns = []
        lines = [line for line in base.transaction_lines(document) if not base.is_skippable(line)]
        for row in base.assemble_rows(lines, bands):
            if row.label == base.OPENING_LABEL:
                opening = base.balance_amount(row.sgd_text, owed_is_negative=True)
            elif row.label in _CLOSING_LABELS:
                closing = base.balance_amount(row.sgd_text, owed_is_negative=True)
            else:
                txns.append(base.build_txn(row, period_start, period_end))

        if opening is None or closing is None:
            raise ParseError("card statement is missing its previous or outstanding balance row")

        account = ParsedAccount(
            account_ref_masked=PRODUCT,
            sub_account_label=None,
            currency=base.BASE_CURRENCY,
            kind=CARD,
            txns=tuple(txns),
            opening_balance_minor=opening,
            closing_balance_minor=closing,
        )

        return ParsedDocument(
            institution=self.institution,
            doc_type=self.doc_type,
            period_start=period_start,
            period_end=period_end,
            statement_date=statement_date,
            parser_version=f"{self.name}@{self.version}",
            accounts=(account,),
        )
