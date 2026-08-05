"""Deciding what a transaction was for.

Stage one of the three in the architecture, and the one that carries most of
the volume: deterministic rules over `counterparty_norm`, versioned in git,
fast and free and explainable. A rule that fires can always be pointed at,
which is what makes a wrong category a fixable bug rather than an argument
with a model.

**The merchant is a strong prior, not an answer.** `IKEA-RESTAURANT` is in this
corpus and is Dining, not Furnishing. So rules match on a pattern and carry a
weight, the most specific match wins, and where two fire with equal claim the
row is left for a human rather than decided by declaration order.

Categories themselves are tenant data, not an enum: `DEFAULT_CATEGORIES` is
only what a new install is seeded with. Nothing here may assume the set is
fixed or that these particular names exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: What a new tenant starts with. Editable per tenant, so this is a seed and
#: never a constraint. See architecture §3.1 for what each holds and why
#: retail is three categories rather than one.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "Grocery",
    "Dining",
    "Transport",
    "Bills and utilities",
    "Insurance",
    "Healthcare",
    "Wellness",
    "Recreation",
    "Travel",
    "Furnishing",
    "Electronics",
    "Fashion",
    "Business services",
    "Others",
)

#: Where a row goes when nothing claims it. Kept in the seed set deliberately:
#: a large Others is the signal that the rules are behind, and hiding it as a
#: null would hide that.
UNCATEGORISED = "Others"


@dataclass(frozen=True, slots=True)
class Rule:
    """One pattern, and what it means.

    `weight` breaks ties between rules that both match. It exists because
    "whose rule wins" is a preference rather than a fact — a household that
    buys most of its lunches at a supermarket may want the opposite default
    from one that does not — and hardcoding precedence would make every such
    disagreement a code change.
    """

    pattern: str
    category: str
    weight: int = 0
    #: Free text for the operator, shown when explaining a decision.
    note: str = ""
    _compiled: re.Pattern = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        try:
            compiled = re.compile(self.pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"rule pattern {self.pattern!r} is not valid: {exc}") from exc
        object.__setattr__(self, "_compiled", compiled)

    def matches(self, counterparty: str) -> bool:
        return bool(self._compiled.search(counterparty or ""))

    @property
    def specificity(self) -> int:
        """How much of a name a rule accounts for.

        A longer pattern is a narrower claim, so `ikea.restaurant` beats
        `ikea` without either needing a weight set by hand. Weight remains the
        override for when that heuristic is wrong.
        """
        return len(self.pattern)


@dataclass(frozen=True, slots=True)
class Decision:
    category: str
    #: The rule that decided it, or None where nothing did.
    rule: Rule | None = None
    #: Rules that matched equally well and were not chosen. Non-empty means the
    #: rules disagree and the row is a question, not an answer.
    contested: tuple[Rule, ...] = ()

    @property
    def is_confident(self) -> bool:
        return self.rule is not None and not self.contested

    @property
    def needs_review(self) -> bool:
        """Either nothing matched, or too much did.

        Both go to the same queue for the same reason: the system does not
        know, and saying so is worth more than a plausible guess that nobody
        will ever check.
        """
        return self.rule is None or bool(self.contested)


def categorise(counterparty: str, rules) -> Decision:
    """The category for one counterparty, and why.

    Ranked by weight first, then by how specific the pattern is. A tie between
    two rules pointing at *different* categories is left undecided — the row
    is contested and goes to review. A tie between rules agreeing on the same
    category is not a disagreement at all, and resolves quietly.
    """
    matched = [rule for rule in rules if rule.matches(counterparty)]
    if not matched:
        return Decision(category=UNCATEGORISED)

    matched.sort(key=lambda r: (r.weight, r.specificity), reverse=True)
    best = matched[0]
    top = (best.weight, best.specificity)

    contested = tuple(
        rule for rule in matched[1:]
        if (rule.weight, rule.specificity) == top and rule.category != best.category
    )
    if contested:
        return Decision(category=UNCATEGORISED, rule=None, contested=(best, *contested))
    return Decision(category=best.category, rule=best)


def coverage(counterparties, rules) -> dict:
    """How much of a ledger the rules actually account for.

    Reported by row rather than by distinct name, because a rule catching one
    merchant seen four hundred times is worth four hundred rules catching one
    each — and the point of stage one is volume.
    """
    decided = contested = 0
    unmatched: dict[str, int] = {}

    for counterparty in counterparties:
        decision = categorise(counterparty, rules)
        if decision.contested:
            contested += 1
        elif decision.rule is not None:
            decided += 1
        else:
            unmatched[counterparty] = unmatched.get(counterparty, 0) + 1

    total = decided + contested + sum(unmatched.values())
    return {
        "rows": total,
        "decided": decided,
        "contested": contested,
        "unmatched": sum(unmatched.values()),
        "share": round(decided / total, 4) if total else 0.0,
        # Ordered by what would pay best to write a rule for next.
        "worst_gaps": sorted(unmatched.items(), key=lambda kv: -kv[1])[:20],
    }
