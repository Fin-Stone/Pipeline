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
from datetime import timedelta

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


def _printed_under(txn) -> int:
    """Which column the statement printed this row in, not which way it went.

    A statement totals its amount columns exactly as printed, and a reversal is
    printed as a negative entry in the column it reverses. So a refund of a
    withdrawal reduces the withdrawal total; it does not appear as a deposit.
    Comparing against those totals has to follow the same rule, or every
    statement containing a reversal disagrees with its own arithmetic.

    Where a layout has a single amount column there is nothing to record, and
    the amount's sign is the only thing the direction can come from.
    """
    return txn.column_sign or (-1 if txn.amount_minor < 0 else 1)


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

        if account.declared_out_minor is not None and account.declared_in_minor is not None:
            out = -sum(t.amount_minor for t in account.txns if _printed_under(t) < 0)
            into = sum(t.amount_minor for t in account.txns if _printed_under(t) > 0)
            if (out, into) != (account.declared_out_minor, account.declared_in_minor):
                failures.append(Failure(
                    account=name,
                    check="declared_totals",
                    detail={
                        "out_parsed_minor": out,
                        "out_stated_minor": account.declared_out_minor,
                        "in_parsed_minor": into,
                        "in_stated_minor": account.declared_in_minor,
                        "out_difference_minor": out - account.declared_out_minor,
                        "in_difference_minor": into - account.declared_in_minor,
                        "txn_count": len(account.txns),
                    },
                ))

        failures.extend(_secondary_checks(account, document, name, amount_ceiling_minor))

    if document.declared_txn_count is not None and document.declared_txn_count != document.txn_count:
        failures.append(Failure(
            account="*",
            check="declared_txn_count",
            detail={"declared": document.declared_txn_count, "parsed": document.txn_count},
        ))

    # `status` describes a document that passed. A failure quarantines it and
    # the caller never reads this, which is why the failure branch here said
    # `imported` — true only in the sense that nothing looked at it.
    status = STATUS_IMPORTED_UNVERIFIED if unverified else STATUS_IMPORTED

    return ValidationResult(
        status=status,
        failures=tuple(failures),
        unverified_accounts=tuple(unverified),
    )


def _ordering_groups(txns) -> list[list]:
    """The rows as the statement grouped them, for the ordering check.

    A statement that files its rows under headings orders each heading on its
    own: MariBank lists a month's repayments before its purchases, and runs
    both newest first. Asserting one ordering across the whole table is then a
    claim about the document that is simply untrue, and it rejected statements
    that had been read correctly.

    Layouts that print one flat table set no section, so every row lands in a
    single group and the check is exactly what it was.
    """
    groups: dict = {}
    for txn in txns:
        groups.setdefault(txn.section, []).append(txn)
    return [g for g in groups.values() if g]


def _direction(dates) -> int:
    """Which way the sequence runs, from its first pair that moves at all."""
    return next((1 if b > a else -1 for a, b in zip(dates, dates[1:]) if a != b), 0)


def _is_ordered(dates) -> bool:
    """True when the dates run consistently one way, either way.

    Newest first is a presentation choice, not disorder: MariBank prints its
    card rows in descending order and reads correctly. What the check is for is
    a row appearing where the table did not put it, which means the table was
    misread — and that shows up as a *change* of direction, whichever direction
    the statement chose.
    """
    return dates == sorted(dates, reverse=_direction(dates) < 0)


def _first_disorder(dates) -> int | None:
    """Where the sequence stops going the way it started."""
    direction = _direction(dates)
    if direction == 0:
        return None
    return next(
        (i for i in range(1, len(dates))
         if (dates[i] - dates[i - 1]).days * direction < 0),
        None,
    )


def _secondary_checks(account, document, name, amount_ceiling_minor) -> list[Failure]:
    failures: list[Failure] = []

    # The window a posting date may land in. Wider than the printed period only
    # where the adapter says its layout posts after the period closes — see
    # `ParsedDocument.posting_grace_days`. Left at zero the check is exactly
    # what it was.
    latest = document.period_end + timedelta(days=document.posting_grace_days)
    outside = [
        t.posted_date.isoformat() for t in account.txns
        if not (document.period_start <= t.posted_date <= latest)
    ]
    if outside:
        failures.append(Failure(
            account=name,
            check="dates_within_period",
            detail={
                "period": [document.period_start.isoformat(), document.period_end.isoformat()],
                "allowed_through": latest.isoformat(),
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
    for group in _ordering_groups(account.txns):
        orderings = {"posted_date": [t.posted_date for t in group]}
        if group and all(t.value_date is not None for t in group):
            orderings["value_date"] = [t.value_date for t in group]

        if any(_is_ordered(dates) for dates in orderings.values()):
            continue

        dates = orderings["posted_date"]
        first_break = _first_disorder(dates)
        failures.append(Failure(
            account=name,
            check="date_monotonicity",
            detail={
                "checked": sorted(orderings),
                "section": group[0].section,
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
