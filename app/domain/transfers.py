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

**The closest pairs are settled first**, across the whole ledger, before any
looser fit is considered. Taking each outflow in turn and giving it the nearest
counterpart still free looks like the same thing and is not: it lets whichever
row comes first take a counterpart that a later row answers exactly, and the
displaced row then takes somebody else's. That cascade is what left several
plainly matching transfers unpaired here — see `_settle`.

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
from collections import Counter
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
    #: Every card number this account has been known by, masked to its last
    #: four. Empty for a deposit account. What lets `Advice Bill Payment CCC -
    #: <digits>` be recognised as settling *this* card rather than some other.
    card_numbers: tuple[str, ...] = ()


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


#: A card number as it turns up inside a description. Three shapes, because
#: three banks in this corpus write it three ways: a solid run of digits, four
#: groups with a separator, and the bank's own masking with only the tail left.
#: Twelve digits at minimum in the unmasked forms, so an account number or a
#: reference cannot pass for a card.
_CARD_RUNS = (
    re.compile(r"(?<!\d)\d{12,19}(?!\d)"),
    re.compile(r"(?<![\d-])(?:\d{4}[ -]){3}\d{4}(?![\d-])"),
    re.compile(r"[Xx*]{4,}[ -]?\d{4}(?!\d)"),
)


def _card_tails(text: str) -> set[str]:
    """The last four of every card-shaped reference in a description."""
    return {
        re.sub(r"\D", "", found)[-4:]
        for pattern in _CARD_RUNS
        for found in pattern.findall(text or "")
    }


def _names_a_card(leg: Leg, other: Leg) -> bool:
    """This row naming a card number the other account has been known by.

    The reason `account_card_number` exists. A card is keyed by its product,
    because the number changes on reissue — but the deposit statement paying
    the bill records the *number*, so without the numbers an account has
    carried, nothing joins the payment to the card. Any of them counts: a
    payment to the number the card had three years ago still settled this card.
    """
    if not other.card_numbers:
        return False
    tails = {number[-4:] for number in other.card_numbers}
    return bool(tails & _card_tails(leg.description))


def _pays_a_card(out_leg: Leg, in_leg: Leg) -> bool:
    """Money leaving a deposit account and landing on a card.

    A card payment is a transfer by construction: the purchases the card made
    are already counted as spending, so counting the payment too overstates the
    month by the size of the bill. That structural fact is evidence in its own
    right and does not need the dates to agree closely.
    """
    return out_leg.account_kind == DEPOSIT and in_leg.account_kind == CARD


def _either_names(out_leg: Leg, in_leg: Leg) -> bool:
    """Either row naming the other's account. Either direction proves the pair."""
    return _names_the_other(out_leg, in_leg) or _names_the_other(in_leg, out_leg)


def _either_names_a_card(out_leg: Leg, in_leg: Leg) -> bool:
    return _names_a_card(out_leg, in_leg) or _names_a_card(in_leg, out_leg)


def _anything(out_leg: Leg, in_leg: Leg) -> bool:
    """The last pass asks for no evidence beyond the amount and the dates."""
    return True


#: The passes, strongest evidence first: what to call it, how far it may reach,
#: and what has to be true of the pair.
#:
#: Order is what makes this safe. A pair where one row names the other's
#: account is settled before anything has to guess, so the certain cases claim
#: their counterparts and the weak pass is left choosing among genuinely
#: unclaimed rows. Reversing it would let a coincidence take the counterpart
#: that a near-proof was about to claim.
_PASSES = (
    ("names the other account", "named_days", _either_names),
    ("names the card it pays", "named_days", _either_names_a_card),
    ("pays a card", "card_days", _pays_a_card),
    ("amount and date", "max_days", _anything),
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

    by_amount: dict[int, list[Leg]] = {}
    for leg in legs:
        if leg.amount_minor > 0:
            by_amount.setdefault(leg.amount_minor, []).append(leg)

    links: list[Link] = []
    claimed: set[int] = set()
    for evidence, window_attr, fits in _PASSES:
        links.extend(_settle(
            outflows, by_amount, claimed, window,
            reach=getattr(window, window_attr), evidence=evidence, fits=fits,
        ))

    return Result(tuple(links), _unsettled(outflows, by_amount, claimed, window))


def _candidates(leg: Leg, by_amount, claimed, window: Window, reach: int, fits):
    """Every free inflow this outflow could answer, with how far apart they sit."""
    for other in by_amount.get(-leg.amount_minor, []):
        if other.txn_id in claimed or other.account_id == leg.account_id:
            continue
        gap = abs((other.posted_date - leg.posted_date).days)
        if window.min_days <= gap <= reach and fits(leg, other):
            yield gap, other


def _settle(outflows, by_amount, claimed, window, *, reach, evidence, fits) -> list[Link]:
    """Pair up one pass's worth of candidates, **closest first**.

    Closest first, not earliest first, and this is the whole of it. Walking the
    outflows in date order and giving each its nearest free counterpart looks
    equivalent and is not: it lets whichever row happens to come first take a
    counterpart that a later row answers exactly, and the displaced row then
    takes somebody else's, and so on down the ledger. On this corpus one
    transfer booked on the 18th claimed the inflow of the 21st; the outflow of
    the 21st — same amount, same day, naming the same account — was pushed onto
    an inflow 30 days later, and the row *that* one answered was left with
    nothing at all. Four rows misread from one greedy choice.

    Distance is the evidence, so it decides the order. A pair booked the same
    day is settled before anything a week apart is considered, and no row can
    be displaced by a worse-fitting claim on it.

    Only a genuine tie is still a judgement call, and the two sides of one are
    not the same question. An outflow that fits two inflows equally well is
    **refused**: nothing distinguishes them, and guessing is what this module
    exists not to do. Two outflows fitting one inflow is decided by date and
    then by id — arbitrary, but the arbitrariness costs nothing, because the
    two are the same amount on the same day and the totals come out identical
    whichever is taken. Refusing there would leave both counted as spending and
    the inflow counted as income, which is the error worth avoiding.

    Settling one contest can leave another uncontested, so each distance is
    worked to a standstill before the next is opened.
    """
    pairs = [
        (gap, leg, other)
        for leg in outflows if leg.txn_id not in claimed
        for gap, other in _candidates(leg, by_amount, claimed, window, reach, fits)
    ]

    made: list[Link] = []
    for gap in sorted({gap for gap, _, _ in pairs}):
        while True:
            free = [
                (out_leg, in_leg) for pair_gap, out_leg, in_leg in pairs
                if pair_gap == gap
                and out_leg.txn_id not in claimed and in_leg.txn_id not in claimed
            ]
            fits_two = Counter(out_leg.txn_id for out_leg, _ in free)
            settled = False
            for out_leg, in_leg in sorted(
                free, key=lambda p: (p[0].posted_date, p[0].txn_id, p[1].txn_id),
            ):
                if fits_two[out_leg.txn_id] > 1:
                    continue
                if out_leg.txn_id in claimed or in_leg.txn_id in claimed:
                    continue
                claimed.update({out_leg.txn_id, in_leg.txn_id})
                made.append(Link(
                    out_txn_id=out_leg.txn_id,
                    in_txn_id=in_leg.txn_id,
                    amount_minor=-out_leg.amount_minor,
                    days_apart=gap,
                    evidence=evidence,
                ))
                settled = True
            if not settled:
                break
    return made


def _unsettled(outflows, by_amount, claimed, window: Window) -> tuple[Ambiguity, ...]:
    """Outflows left over that fit more than one counterpart equally well.

    Read off the end rather than as each pass runs: an ambiguity in an early
    pass is not final, because a later one may settle the leg that was in the
    way. Only what survives every pass is worth asking a person about.
    """
    found = []
    for leg in sorted(outflows, key=lambda x: (x.posted_date, x.txn_id)):
        if leg.txn_id in claimed:
            continue
        gaps = list(_candidates(
            leg, by_amount, claimed, window, window.max_days, _anything,
        ))
        if not gaps:
            continue
        nearest = min(gap for gap, _ in gaps)
        tied = [other for gap, other in gaps if gap == nearest]
        if len(tied) > 1:
            found.append(Ambiguity(
                txn_id=leg.txn_id,
                amount_minor=leg.amount_minor,
                posted_date=leg.posted_date,
                candidate_txn_ids=tuple(sorted(other.txn_id for other in tied)),
            ))
    return tuple(found)


def window_around(day: date, days: int = DEFAULT_WINDOW_DAYS) -> tuple[date, date]:
    """The dates a counterpart may carry, for narrowing a query."""
    return day - timedelta(days=days), day + timedelta(days=days)


#: The old name. Kept because it was the module's export before `Window` needed
#: the word more.
window = window_around
