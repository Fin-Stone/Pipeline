"""Running the transfer matcher against a stored ledger, and saying what moved.

The matcher itself is a pure function in `app.domain.transfers` and knows
nothing about databases. This is the thin piece that feeds it rows, remembers
the window the operator chose, and — the part that matters — reports the
*difference* between what it found and what is already recorded.

**"206 links" tells nobody whether to apply it. "9 new, 2 gone" is the whole
decision.** A re-run replaces every automatic link, so an operator asked to
approve one needs to know what they are giving up as well as what they gain.

This also exists so that a re-run is cheap enough to be automatic. Half the
card payments the matcher misses are missing nothing but the other statement:
the payment is on the card and the savings account it came from has not been
imported yet. Nothing can pair them at that moment, and everything can the
instant the second document lands — so the pass runs after every import rather
than waiting for somebody to remember it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..domain.models import DEPOSIT
from ..domain.transfers import Leg, Result, Window, find_transfers

#: Setting keys, and the `Window` field each one fills. Prefixed because they
#: share a table with every other choice a tenant makes.
SETTING_KEYS = {
    "transfer.min_days": "min_days",
    "transfer.max_days": "max_days",
    "transfer.named_days": "named_days",
    "transfer.card_days": "card_days",
}


def window_for(repository, context) -> Window:
    """The window this tenant chose, falling back to the defaults per field.

    Per field, not all-or-nothing: an operator who widened only the card window
    should keep the improved defaults for everything else, and a setting stored
    before a field existed must not stop the new one working.
    """
    stored = repository.get_settings(context)
    chosen = {}
    for key, field in SETTING_KEYS.items():
        if key in stored:
            try:
                chosen[field] = int(stored[key])
            except ValueError:
                # A value nothing can parse is not worth failing a dashboard
                # over. The default is a working answer; a 500 is not.
                continue
    return Window(**chosen)


def save_window(repository, context, window: Window, defaults: Window | None = None) -> None:
    """Store a window, keeping only what differs from the defaults.

    Storing a value equal to the default would pin this install to it forever,
    and a later improvement to that default would reach nobody.
    """
    defaults = defaults or Window()
    repository.set_settings(context, {
        key: (
            None if getattr(window, field) == getattr(defaults, field)
            else str(getattr(window, field))
        )
        for key, field in SETTING_KEYS.items()
    })


def _legs(repository, context) -> list[Leg]:
    return [
        Leg(
            txn_id=row["id"],
            account_id=row["account_id"],
            account_ref=row["account_ref_masked"],
            posted_date=row["posted_date"],
            amount_minor=row["amount_minor"],
            description=row["description_raw"],
            account_kind=row.get("kind") or DEPOSIT,
            card_numbers=tuple(row.get("card_numbers") or ()),
        )
        for row in repository.list_transfer_legs(context)
    ]


@dataclass(frozen=True, slots=True)
class Realignment:
    """What a re-run found, and what it would change."""

    window: Window
    result: Result
    #: Pairs the matcher found that are not recorded.
    added: tuple[tuple[int, int], ...] = ()
    #: Automatic links currently recorded that this run would not make. Named
    #: separately from `added` because they are what an operator is being asked
    #: to *give up*, and a single net number would hide them.
    removed: tuple[tuple[int, int], ...] = ()
    unchanged: int = 0
    #: Manual marks, which a re-run never touches. Reported so the count on
    #: screen adds up against `GET /transfers`.
    manual: int = 0
    applied: bool = False

    @property
    def value_minor(self) -> int:
        """What these links keep out of spending. Positive."""
        return sum(link.amount_minor for link in self.result.links)

    @property
    def by_evidence(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for link in self.result.links:
            counts[link.evidence] = counts.get(link.evidence, 0) + 1
        return counts


def _spoken_for(stored) -> set[int]:
    """Transactions the operator has already claimed by hand.

    These are withheld from the matcher entirely, rather than merely having
    their links left in place. "A re-run never touches a manual mark" has to
    mean the *rows* are out of scope: a transaction belongs to at most one
    movement, so a matcher free to pair a marked row produces a second claim on
    it, and the unique constraint that enforces "at most one" rejects the whole
    batch. The dashboard then fails with a database error naming a table the
    operator has never heard of — which is what happened.

    Withheld and not merely skipped afterwards, because the counterpart matters
    too: dropping the offending link at the end would leave whichever row it
    displaced unpaired for no reason it could explain.
    """
    return {
        txn_id
        for link in stored if link["origin"] != "auto"
        for txn_id in (link["out_txn_id"], link["in_txn_id"])
        if txn_id is not None
    }


def preview(repository, context, window: Window | None = None) -> Realignment:
    """Run the matcher and diff it against what is stored. Writes nothing."""
    window = window or window_for(repository, context)
    stored = repository.list_transfer_links(context)

    spoken_for = _spoken_for(stored)
    legs = [leg for leg in _legs(repository, context) if leg.txn_id not in spoken_for]
    result = find_transfers(legs, window=window)

    # Only automatic links are comparable. A manual mark is the operator's
    # claim, is not regenerable, and a re-run leaves it alone — counting one as
    # "removed" would offer to undo something this pass will not touch.
    existing = {(link["out_txn_id"], link["in_txn_id"]) for link in stored if link["origin"] == "auto"}
    found = {(link.out_txn_id, link.in_txn_id) for link in result.links}

    return Realignment(
        window=window,
        result=result,
        added=tuple(sorted(found - existing)),
        removed=tuple(sorted(existing - found)),
        unchanged=len(found & existing),
        manual=sum(1 for link in stored if link["origin"] != "auto"),
    )


def realign(repository, context, window: Window | None = None) -> Realignment:
    """Run it and record the result, replacing every automatic link.

    Replace rather than add, for the reason `replace_transfer_links` gives:
    the pass is a pure function of the ledger, so running it twice must leave
    the same answer.
    """
    outcome = preview(repository, context, window)
    repository.replace_transfer_links(context, outcome.result.links)
    return replace(outcome, applied=True)
