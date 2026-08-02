"""Statement validation.

The statement carries its own checksum. Using it is worth more than any amount
of parser cleverness: one comparison catches dropped rows, duplicated rows,
sign errors, misread digits and column misalignment.

    opening_balance_minor + sum(amount_minor) == closing_balance_minor

Because amounts are signed by their effect on the account balance, and card
balances are stored negated, this single formula covers deposit and card
statements alike.

A failure rejects the **entire document**, not the offending account. Partial
imports produce a ledger that looks fine and is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.models import ParsedAccount, ParsedDocument
from ..ports.repository import STATUS_IMPORTED, STATUS_IMPORTED_UNVERIFIED


@dataclass(frozen=True, slots=True)
class Failure:
    account: str
    check: str
    detail: dict


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: str
    failures: tuple[Failure, ...] = ()
    unverified_accounts: tuple[str, ...] = ()
    notes: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures


def _label(account: ParsedAccount) -> str:
    if account.sub_account_label:
        return f"{account.account_ref_masked}/{account.sub_account_label}"
    return account.account_ref_masked


def validate(document: ParsedDocument, *, amount_ceiling_minor: int) -> ValidationResult:
    failures: list[Failure] = []
    unverified: list[str] = []

    for account in document.accounts:
        name = _label(account)

        if account.has_balances:
            total = sum(t.amount_minor for t in account.txns)
            expected = account.opening_balance_minor + total
            if expected != account.closing_balance_minor:
                failures.append(Failure(
                    account=name,
                    check="balance_reconciliation",
                    detail={
                        "opening_minor": account.opening_balance_minor,
                        "transactions_sum_minor": total,
                        "expected_closing_minor": expected,
                        "stated_closing_minor": account.closing_balance_minor,
                        "difference_minor": expected - account.closing_balance_minor,
                        "txn_count": len(account.txns),
                    },
                ))
        else:
            # Not a failure: some formats simply do not state balances. It is
            # recorded so the document is never mistaken for a reconciled one.
            unverified.append(name)

        failures.extend(_secondary_checks(account, document, name, amount_ceiling_minor))

    if document.declared_txn_count is not None and document.declared_txn_count != document.txn_count:
        failures.append(Failure(
            account="*",
            check="declared_txn_count",
            detail={"declared": document.declared_txn_count, "parsed": document.txn_count},
        ))

    if failures:
        status = STATUS_IMPORTED  # unused; the caller quarantines on failures
    elif unverified:
        status = STATUS_IMPORTED_UNVERIFIED
    else:
        status = STATUS_IMPORTED

    return ValidationResult(
        status=status,
        failures=tuple(failures),
        unverified_accounts=tuple(unverified),
    )


def _secondary_checks(account, document, name, amount_ceiling_minor) -> list[Failure]:
    failures: list[Failure] = []

    outside = [
        t.posted_date.isoformat() for t in account.txns
        if not (document.period_start <= t.posted_date <= document.period_end)
    ]
    if outside:
        failures.append(Failure(
            account=name,
            check="dates_within_period",
            detail={
                "period": [document.period_start.isoformat(), document.period_end.isoformat()],
                "outside": outside[:20],
                "outside_count": len(outside),
            },
        ))

    # Rows must be in order by *one* of the dates the statement prints, not
    # necessarily the posting date.
    #
    # Card statements that carry both a transaction and a posting date are
    # ordered by the transaction date, so posting dates legitimately go
    # backwards: something bought on 29 Sep can post on 3 October while
    # something bought on 30 Sep posts on the 2nd. Requiring monotonic posting
    # dates rejected eleven statements that reconciled to the cent — the check
    # was wrong, not the documents.
    #
    # The point of the check is to notice rows read out of sequence, which
    # would mean the table was misread. Ordering by either printed date
    # satisfies that.
    orderings = {"posted_date": [t.posted_date for t in account.txns]}
    if all(t.value_date is not None for t in account.txns) and account.txns:
        orderings["value_date"] = [t.value_date for t in account.txns]

    if not any(dates == sorted(dates) for dates in orderings.values()):
        dates = orderings["posted_date"]
        first_break = next((i for i in range(1, len(dates)) if dates[i] < dates[i - 1]), None)
        failures.append(Failure(
            account=name,
            check="date_monotonicity",
            detail={
                "checked": sorted(orderings),
                "first_out_of_order_index": first_break,
                "at": dates[first_break].isoformat() if first_break is not None else None,
            },
        ))

    oversized = [
        {"description": t.description_raw[:80], "amount_minor": t.amount_minor}
        for t in account.txns
        if abs(t.amount_minor) > amount_ceiling_minor
    ]
    if oversized:
        failures.append(Failure(
            account=name,
            check="amount_ceiling",
            detail={"ceiling_minor": amount_ceiling_minor, "rows": oversized[:10]},
        ))

    return failures
