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

**The window widens with the strength of the evidence.** Amount and date alone
is the weakest claim two rows can make — on a household ledger the same round
number turns up constantly — so it gets the narrowest window, and beyond a few
days the amounts start colliding with unrelated spending. One leg naming the
other's account number is near-proof and can afford weeks. A deposit paying a
card is structurally a transfer whatever the dates say, and card issuers post
on their own cycle, so it gets its own window too. Using one window for all
three would mean either missing the card payments or guessing at the
coincidences.

Where two candidates fit equally well the pair is **refused, not guessed**. Two
identical transfers on one day are indistinguishable, and a wrong link is worse
than a missing one: it silently removes real spending from the total, which is
the failure this whole pass exists to prevent. Refusals are reported so they can
be settled by eye.

**A pair that cannot be made today may become obvious tomorrow.** Half the card
payments this misses are missing nothing but the other statement — the payment
is on the card and the savings account it came from has not been imported yet.
Nothing here can know that. What matters is that the pass is cheap, pure, and
rebuilt from the ledger every time a document arrives, so the link appears the
moment the evidence does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .models import CARD, DEPOSIT

#: How far apart two rows may be booked to pair on amount and date alone. A
#: FAST transfer lands the same day; a working day or two of settlement and a
#: weekend covers nearly all of the rest. This is the weakest evidence there
#: is, so it gets the tightest window.
DEFAULT_WINDOW_DAYS = 4

#: The same, for one leg naming the other's account number. Near-proof, so the
#: dates barely matter and only absurd distances are refused.
DEFAULT_NAMED_WINDOW_DAYS = 30

#: The same, for money leaving a deposit account and landing on a card. Paying
#: a card is a transfer by construction, and issuers post on a statement cycle
#: rather than on the day you paid.
DEFAULT_CARD_WINDOW_DAYS = 21

#: The smallest gap allowed. Zero by default, because most transfers land the
#: same day. Raise it if same-day coincidences are being paired on a ledger
#: with a lot of round numbers.
DEFAULT_MIN_DAYS = 0


@dataclass(frozen=True, slots=True)
class Window:
    """How far apart two legs may be booked, per kind of evidence.

    One object rather than four arguments because these are read together,
    stored together, and shown to the operator together — and because a caller
    that could pass the card window where the named window belongs would do so
    eventually.
    """

    min_days: int = DEFAULT_MIN_DAYS
    max_days: int = DEFAULT_WINDOW_DAYS
    named_days: int = DEFAULT_NAMED_WINDOW_DAYS
    card_days: int = DEFAULT_CARD_WINDOW_DAYS

    def __post_init__(self) -> None:
        if self.min_days < 0:
            raise ValueError("min_days cannot be negative")
        widest = max(self.max_days, self.named_days, self.card_days)
        if self.min_days > widest:
            raise ValueError(
                f"min_days {self.min_days} is beyond every window "
                f"(the widest is {widest}), so nothing could ever pair"
            )

    @property
    def furthest(self) -> int:
        """The widest any pass reaches. For narrowing a query."""
        return max(self.max_days, self.named_days, self.card_days)


@dataclass(frozen=True, slots=True)
class Leg:
    """One side of a candidate transfer, as the ledger holds it."""

    txn_id: int
    account_id: int
    account_ref: str
    posted_date: date
    amount_minor: int
    description: str
    #: `deposit` or `card`. Money leaving a deposit and landing on a card is a
    #: payment, and that is evidence rather than a coincidence — see `find_transfers`.
    account_kind: str = DEPOSIT


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


def _pays_a_card(out_leg: Leg, in_leg: Leg) -> bool:
    """Money leaving a deposit account and landing on a card.

    A card payment is a transfer by construction: the purchases the card made
    are already counted as spending, so counting the payment too overstates the
    month by the size of the bill. That structural fact is evidence in its own
    right and does not need the dates to agree closely.
    """
    return out_leg.account_kind == DEPOSIT and in_leg.account_kind == CARD


#: The passes, strongest evidence first, and how far each may reach.
#:
#: Order is what makes this safe. A pair where one row names the other's
#: account is settled before anything has to guess, so the certain cases claim
#: their counterparts and the weak pass is left choosing among genuinely
#: unclaimed rows. Reversing it would let a coincidence take the counterpart
#: that a near-proof was about to claim.
_PASSES = (
    ("names the other account", "named_days"),
    ("pays a card", "card_days"),
    ("amount and date", "max_days"),
)


def find_transfers(
    legs,
    *,
    window: Window | None = None,
    window_days: int | None = None,
) -> Result:
    """Pair each outflow with the inflow that answers it.

    `window_days` is the older single-window form and still works: it sets the
    amount-and-date window and leaves the other two at their defaults. Callers
    that care about the difference pass a `Window`.
    """
    if window is None:
        window = Window(max_days=window_days) if window_days is not None else Window()

    legs = list(legs)
    outflows = [leg for leg in legs if leg.amount_minor < 0]
    inflows = [leg for leg in legs if leg.amount_minor > 0]

    by_amount: dict[int, list[Leg]] = {}
    for leg in inflows:
        by_amount.setdefault(leg.amount_minor, []).append(leg)

    links: list[Link] = []
    ambiguous: list[Ambiguity] = []
    claimed: set[int] = set()

    for evidence, window_attr in _PASSES:
        reach = getattr(window, window_attr)
        # Ordered by date so the outcome does not depend on the order rows came
        # out of the database.
        for leg in sorted(outflows, key=lambda x: (x.posted_date, x.txn_id)):
            if leg.txn_id in claimed:
                continue

            candidates = [
                other for other in by_amount.get(-leg.amount_minor, [])
                if other.txn_id not in claimed
                and other.account_id != leg.account_id
                and window.min_days
                <= abs((other.posted_date - leg.posted_date).days)
                <= reach
            ]
            if evidence == "names the other account":
                candidates = [
                    other for other in candidates
                    if _names_the_other(leg, other) or _names_the_other(other, leg)
                ]
            elif evidence == "pays a card":
                candidates = [other for other in candidates if _pays_a_card(leg, other)]
            if not candidates:
                continue

            best = _closest(candidates, leg.posted_date)
            if len(best) > 1:
                # Reported only from the last pass. An ambiguity in an earlier
                # one is not final — a weaker pass with a different window may
                # still settle it, and reporting it here would ask the operator
                # to adjudicate something the next pass is about to answer.
                if evidence == _PASSES[-1][0]:
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
                evidence=evidence,
            ))

    return Result(tuple(links), tuple(ambiguous))


def _closest(candidates: list[Leg], to: date) -> list[Leg]:
    """The candidates booked nearest the other leg, all of them if tied."""
    nearest = min(abs((c.posted_date - to).days) for c in candidates)
    return [c for c in candidates if abs((c.posted_date - to).days) == nearest]


def window_around(day: date, days: int = DEFAULT_WINDOW_DAYS) -> tuple[date, date]:
    """The dates a counterpart may carry, for narrowing a query."""
    return day - timedelta(days=days), day + timedelta(days=days)


#: The old name. Kept because it was the module's export before `Window` needed
#: the word more.
window = window_around
