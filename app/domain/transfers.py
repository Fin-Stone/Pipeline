"""Pairing the two legs of a movement between the household's own accounts.

A transfer is recorded twice, once on each side, and both records are correct:
money did leave one account and arrive in another. What is wrong is counting
either as spending. Paying a credit card off is the clearest case — the payment
leaves a savings account and settles the card, and the purchases the card made
are already spending. Count the payment too and the month is wrong by the size
of the bill.

**Nothing here changes a transaction.** A link is a claim *about* two rows, held
beside them and rebuildable at any time, because the rule for what counts as a
transfer is a judgement that will change while the ledger's contents must not.

What makes a pair:

- opposite signs, equal magnitude, in one tenant and across two accounts;
- posted within a few days of each other, because the two banks book it on
  their own schedules;
- and, where the statement says so, one leg naming the other's account.

Where two candidates fit equally well the pair is **refused, not guessed**. Two
identical transfers on one day are indistinguishable, and a wrong link is worse
than a missing one: it silently removes real spending from the total, which is
the failure this whole pass exists to prevent. Refusals are reported so they can
be settled by eye.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

#: How far apart the two legs may be booked. A FAST transfer lands the same
#: day; a card payment can take a working day or two to post; a weekend adds
#: another. Beyond this the amounts start colliding with unrelated spending.
DEFAULT_WINDOW_DAYS = 4


@dataclass(frozen=True, slots=True)
class Leg:
    """One side of a candidate transfer, as the ledger holds it."""

    txn_id: int
    account_id: int
    account_ref: str
    posted_date: date
    amount_minor: int
    description: str


@dataclass(frozen=True, slots=True)
class Link:
    """Two legs proven to be one movement."""

    out_txn_id: int
    in_txn_id: int
    amount_minor: int
    days_apart: int
    #: Why this pair was accepted, for the operator to audit rather than trust.
    evidence: str


@dataclass(frozen=True, slots=True)
class Ambiguity:
    """A leg that fits more than one counterpart equally well."""

    txn_id: int
    amount_minor: int
    posted_date: date
    candidate_txn_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Result:
    links: tuple[Link, ...] = ()
    ambiguous: tuple[Ambiguity, ...] = ()

    @property
    def linked_txn_ids(self) -> frozenset[int]:
        return frozenset(
            i for link in self.links for i in (link.out_txn_id, link.in_txn_id)
        )


def _digits(text: str) -> set[str]:
    """Account-shaped runs of digits in a description.

    DBS writes the far account into the row: a transfer out of savings reads
    "Funds Transfer 98-7654321-0", and the matching row on the other account
    names the first. Short runs are excluded because a reference number would
    otherwise look like an account.
    """
    return {re.sub(r"\D", "", token) for token in re.findall(r"[\d-]{6,}", text or "")}


def _names_the_other(leg: Leg, other: Leg) -> bool:
    reference = re.sub(r"\D", "", other.account_ref or "")
    return bool(reference) and reference in _digits(leg.description)


def find_transfers(legs, *, window_days: int = DEFAULT_WINDOW_DAYS) -> Result:
    """Pair each outflow with the inflow that answers it.

    Legs are matched strongest-evidence first: a pair where one row names the
    other's account is settled before a pair that only agrees on amount and
    date, so the unambiguous cases claim their counterparts before the
    ambiguous ones have to guess at what is left.
    """
    legs = list(legs)
    outflows = [leg for leg in legs if leg.amount_minor < 0]
    inflows = [leg for leg in legs if leg.amount_minor > 0]

    by_amount: dict[int, list[Leg]] = {}
    for leg in inflows:
        by_amount.setdefault(leg.amount_minor, []).append(leg)

    links: list[Link] = []
    ambiguous: list[Ambiguity] = []
    claimed: set[int] = set()

    # Named-account pairs first, then the rest. Both passes are ordered by date
    # so the outcome does not depend on the order rows came out of the database.
    for named_only in (True, False):
        for leg in sorted(outflows, key=lambda x: (x.posted_date, x.txn_id)):
            if leg.txn_id in claimed:
                continue

            candidates = [
                other for other in by_amount.get(-leg.amount_minor, [])
                if other.txn_id not in claimed
                and other.account_id != leg.account_id
                and abs((other.posted_date - leg.posted_date).days) <= window_days
            ]
            if named_only:
                candidates = [
                    other for other in candidates
                    if _names_the_other(leg, other) or _names_the_other(other, leg)
                ]
            if not candidates:
                continue

            best = _closest(candidates, leg.posted_date)
            if len(best) > 1:
                if not named_only:
                    ambiguous.append(Ambiguity(
                        txn_id=leg.txn_id,
                        amount_minor=leg.amount_minor,
                        posted_date=leg.posted_date,
                        candidate_txn_ids=tuple(sorted(c.txn_id for c in best)),
                    ))
                continue

            other = best[0]
            claimed.update({leg.txn_id, other.txn_id})
            links.append(Link(
                out_txn_id=leg.txn_id,
                in_txn_id=other.txn_id,
                amount_minor=-leg.amount_minor,
                days_apart=abs((other.posted_date - leg.posted_date).days),
                evidence="names the other account" if named_only else "amount and date",
            ))

    return Result(tuple(links), tuple(ambiguous))


def _closest(candidates: list[Leg], to: date) -> list[Leg]:
    """The candidates booked nearest the other leg, all of them if tied."""
    nearest = min(abs((c.posted_date - to).days) for c in candidates)
    return [c for c in candidates if abs((c.posted_date - to).days) == nearest]


def window(day: date, days: int = DEFAULT_WINDOW_DAYS) -> tuple[date, date]:
    """The dates a counterpart may carry, for narrowing a query."""
    return day - timedelta(days=days), day + timedelta(days=days)
