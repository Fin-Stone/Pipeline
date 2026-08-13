"""Checking the ledger against the balances the banks declared.

Validation at import proves one document consistent with itself:
`opening + Σ movements == closing`, inside that statement, at the moment it was
parsed. That is the strongest single check in the system and it cannot see the
one failure that matters most afterwards — **what happened between statements,
and what the ledger did with rows two statements both claimed.**

Two accounts of the same money, compared:

- what the banks said, statement to statement — `closing` on one, `opening` on
  the next, and the distance between two closings;
- what the ledger holds — the transactions dated in that span, each counted
  once no matter how many statements delivered it.

Where those disagree, the ledger is lying, and architecture §8.2 is blunt about
the deadline: you want to know that month, not next year. This is what the
`seq` limitation in `dedupe` refers to when it says drift will be caught — a
row imported twice from two overlapping statements shows up here as movement
the banks never declared.

Nothing here writes. It is a reading, and the answer to a real drift is a
person looking at the two statements, not an automatic correction: a ledger
that quietly adjusts itself to match a number it cannot explain has stopped
being a record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

#: A statement declaring neither balance says nothing to compare against. Some
#: layouts genuinely do not print them — those documents import as
#: `imported_unverified` — and they are skipped rather than read as zero.
CONTINUITY = "continuity"
MOVEMENT = "movement"


@dataclass(frozen=True, slots=True)
class Declaration:
    """One account's opening and closing balance, as one statement stated it."""

    account_id: int
    account: str
    period_start: date
    period_end: date
    opening_balance_minor: int | None
    closing_balance_minor: int | None


@dataclass(frozen=True, slots=True)
class Drift:
    """One disagreement between the banks' arithmetic and the ledger's."""

    account_id: int
    account: str
    kind: str
    #: The span the disagreement is about: from the close of one statement to
    #: the close of the next. Named so a person can open the two documents.
    since: date
    until: date
    declared_minor: int
    observed_minor: int

    @property
    def difference_minor(self) -> int:
        return self.observed_minor - self.declared_minor


def drifts(declarations, movements) -> list[Drift]:
    """Every disagreement, oldest first.

    `movements` is `{(account_id, date): total_minor}` — the ledger's own
    movement per account per day, each transaction counted once. Summing it
    across a span is the pipeline's claim about that span; the declarations are
    the banks' claim about the same one.

    **Consecutive statements only, and only where both ends declared.** A gap
    in the corpus — a statement nobody has imported yet — is not drift, and
    reporting it as such would bury the real thing under noise about documents
    the operator knows are missing. What the gap does mean is that the check
    cannot see inside it, which is why the span travels with every finding.
    """
    found: list[Drift] = []
    for account_id, rows in _by_account(declarations):
        for earlier, later in zip(rows, rows[1:]):
            if earlier.closing_balance_minor is None:
                continue

            # The banks' own two claims about the same instant. A mismatch here
            # is upstream of the ledger entirely: either a statement is missing
            # between these two, or one of them is not about the account the
            # other is.
            if (
                later.opening_balance_minor is not None
                and later.opening_balance_minor != earlier.closing_balance_minor
            ):
                found.append(Drift(
                    account_id=account_id, account=later.account, kind=CONTINUITY,
                    since=earlier.period_end, until=later.period_start,
                    declared_minor=earlier.closing_balance_minor,
                    observed_minor=later.opening_balance_minor,
                ))

            if later.closing_balance_minor is None:
                continue

            # What the ledger says happened between the two closings, against
            # what the two closings say happened. This is the half that catches
            # a row imported twice.
            declared = later.closing_balance_minor - earlier.closing_balance_minor
            observed = sum(
                total for (acc, on), total in movements.items()
                if acc == account_id and earlier.period_end < on <= later.period_end
            )
            if observed != declared:
                found.append(Drift(
                    account_id=account_id, account=later.account, kind=MOVEMENT,
                    since=earlier.period_end, until=later.period_end,
                    declared_minor=declared, observed_minor=observed,
                ))

    return sorted(found, key=lambda d: (d.until, d.account, d.kind))


def _by_account(declarations):
    """Declarations grouped by account, each run ordered by when it closed.

    Sorted here rather than trusted from the caller: the comparison is between
    *neighbours*, so an unsorted run would compare a January closing with a
    June opening and report drift on a ledger that is perfectly fine.
    """
    grouped: dict[int, list[Declaration]] = {}
    for row in declarations:
        grouped.setdefault(row.account_id, []).append(row)
    for account_id in sorted(grouped):
        yield account_id, sorted(grouped[account_id], key=lambda d: (d.period_end, d.period_start))
