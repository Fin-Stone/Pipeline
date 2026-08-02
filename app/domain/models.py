"""The shapes an adapter returns and the pipeline consumes.

All frozen: a parsed document is a fact about a file's contents, and nothing
downstream has any business mutating it.

Sign convention, decided once and applied everywhere: `amount_minor` is signed
by its effect on the account balance as the statement presents it. Money in is
positive, money out is negative. A card purchase is negative; a payment to the
card is positive. Card statements are stored with
`opening = -previous_outstanding` and `closing = -current_outstanding`, which
collapses deposit and card statements into a single validator formula:

    opening_balance_minor + sum(amount_minor) == closing_balance_minor

Adapters normalise into this convention at their boundary. Nothing downstream
needs to know which formula the institution printed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

DEPOSIT = "deposit"
CARD = "card"
ACCOUNT_KINDS = (DEPOSIT, CARD)

DOC_TYPE_ACCOUNT = "acc"
DOC_TYPE_CARD = "cc"


@dataclass(frozen=True, slots=True)
class ParsedTxn:
    posted_date: date
    amount_minor: int
    currency: str
    description_raw: str
    value_date: date | None = None
    # Populated only for foreign-currency rows: the amount as originally
    # billed, plus the rate the institution settled it at. `amount_minor`
    # always remains the settled amount, so reconciliation is unaffected.
    fx_amount_minor: int | None = None
    fx_currency: str | None = None
    fx_rate: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            raise TypeError("amount_minor must be an int in minor units, never a float")


@dataclass(frozen=True, slots=True)
class ParsedAccount:
    """One account's worth of a statement.

    A single document may carry several of these — Trust savings statements
    contain one per pocket, each with its own opening and closing balance, and
    some card statements cover several cards at once.

    `account_ref_masked` is the account's stable identity: the masked account
    number for a deposit account, and the card *product* for a card. Card
    numbers change on reissue while the account continues, so keying on the
    number would fork one account's history in two; the product is what
    actually distinguishes two cards held at the same bank.
    """

    account_ref_masked: str
    currency: str
    kind: str
    txns: tuple[ParsedTxn, ...] = ()
    sub_account_label: str | None = None
    opening_balance_minor: int | None = None
    closing_balance_minor: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in ACCOUNT_KINDS:
            raise ValueError(f"unknown account kind {self.kind!r}")

    @property
    def natural_key(self) -> str:
        """Database-independent identity, used for dedupe keys.

        Deliberately the natural key rather than a surrogate id: dedupe keys
        computed from it survive a dump, a restore, or a move to a different
        database engine.
        """
        return "|".join([
            self.account_ref_masked,
            self.sub_account_label or "",
            self.currency,
        ])

    @property
    def has_balances(self) -> bool:
        return self.opening_balance_minor is not None and self.closing_balance_minor is not None


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    institution: str
    doc_type: str
    period_start: date
    period_end: date
    parser_version: str
    accounts: tuple[ParsedAccount, ...] = ()
    statement_date: date | None = None
    declared_txn_count: int | None = None

    @property
    def txn_count(self) -> int:
        return sum(len(a.txns) for a in self.accounts)


@dataclass(frozen=True, slots=True)
class StagedFile:
    """A file discovered in uploads/ and copied into the inbox."""

    sha256: str
    inbox_path: str
    source_profile: str
    source_relpath: str


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    sha256: str
    path: str
    status: str          # imported | imported_unverified | duplicate | quarantined
    reason: str | None = None
    documents: int = 0
    accounts: int = 0
    txns_inserted: int = 0
    txns_skipped: int = 0
    detail: dict = field(default_factory=dict)
