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
    #: Bank and card charges: fees, interest, FX margins, late payment. Real
    #: expenditure that belongs to no merchant, so without a home of its own it
    #: sits in Others permanently however many rules get written — and it is
    #: the one category a household can act on directly.
    "Fees and charges",
    "Others",
)

#: Where a row goes when nothing claims it. Kept in the seed set deliberately:
#: a large Others is the signal that the rules are behind, and hiding it as a
#: null would hide that.
UNCATEGORISED = "Others"


#: A pattern that is `^...$` around nothing but an escaped literal. That is
#: what `operator_rule` produces, so on a working install almost every rule is
#: one — see RuleSet.
_ANCHORED = re.compile(r"\A\^(.*)\$\Z", re.DOTALL)
_UNESCAPE = re.compile(r"\\(.)", re.DOTALL)


def _literal_of(pattern: str) -> str | None:
    """The whole name `pattern` matches, if it matches exactly one name.

    Decided by round-trip rather than by inspection: unescape the body, escape
    it again, and accept only if that reproduces the original. Anything with
    real regex in it fails that and is left to be scanned.
    """
    anchored = _ANCHORED.match(pattern)
    if not anchored:
        return None
    body = anchored.group(1)
    try:
        plain = _UNESCAPE.sub(r"\1", body)
    except re.error:  # pragma: no cover - sub on a literal cannot fail
        return None
    if re.escape(plain) != body:
        return None
    # `$` also matches before a trailing newline, which a dict lookup would
    # not. Names are normalised and never contain one, but the fast path has
    # to be exactly equivalent or it is not a fast path, it is a bug.
    return plain.upper() if "\n" not in plain else None


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
    #: The whole name this rule matches, upper-cased, when its pattern is an
    #: anchored literal and nothing more. `None` for a rule that is a real
    #: expression. See RuleSet for what this buys.
    literal: str | None = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        try:
            compiled = re.compile(self.pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"rule pattern {self.pattern!r} is not valid: {exc}") from exc
        object.__setattr__(self, "_compiled", compiled)
        object.__setattr__(self, "literal", _literal_of(self.pattern))

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


class RuleSet(tuple):
    """Rules, with the exact-match ones indexed by the name they match.

    A tuple, so it can be passed anywhere a list of rules already goes and
    nothing else has to know it exists.

    **Why.** A decided counterparty becomes `^<escaped name>$` — see
    `operator_rule` — so on a working install nearly every rule matches exactly
    one name and is a dictionary key wearing a regex costume. Scanning all of
    them against every name made `/review` run 536 patterns over 1,131 distinct
    counterparties: 600,000 regex searches to answer a question that is mostly
    a lookup, and the endpoint took a quarter of a second doing it.

    Rules that are genuinely expressions are still scanned. The answer is
    identical either way; only the work differs.
    """

    # No __slots__: a tuple subclass cannot have them, and the two attributes
    # below live in an instance dict instead.

    def __new__(cls, rules=()):
        self = super().__new__(cls, rules)
        exact: dict[str, list] = {}
        general: list = []
        for rule in self:
            if rule.literal is None:
                general.append(rule)
            else:
                exact.setdefault(rule.literal, []).append(rule)
        self._exact = exact
        self._general = general
        return self

    def matching(self, counterparty: str) -> list:
        name = counterparty or ""
        found = list(self._exact.get(name.upper(), ()))
        found.extend(rule for rule in self._general if rule._compiled.search(name))
        return found


def categorise(counterparty: str, rules) -> Decision:
    """The category for one counterparty, and why.

    Ranked by weight first, then by how specific the pattern is. A tie between
    two rules pointing at *different* categories is left undecided — the row
    is contested and goes to review. A tie between rules agreeing on the same
    category is not a disagreement at all, and resolves quietly.

    Pass a `RuleSet` when categorising many names; a plain sequence still works
    and is scanned.
    """
    if isinstance(rules, RuleSet):
        matched = rules.matching(counterparty)
    else:
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


#: Buckets a representative amount is snapped to before it leaves the
#: household. Coarse on purpose, and present for *capability* rather than
#: privacy: the magnitude is what separates IKEA at 3.50 from IKEA at 890,
#: which is the case already sitting in this corpus.
AMOUNT_GATES: tuple[int, ...] = (
    100, 500, 1_000, 2_000, 5_000, 10_000,
    30_000, 50_000, 100_000, 1_000_000, 5_000_000,
)

#: Marks of a counterparty that is a person or a bare account rather than a
#: merchant. These are the leak that looks like protection if it is missed:
#: scrubbing the operator's own identifiers while sending the names of the
#: people they pay inverts the protection entirely.
_PERSONAL = re.compile(
    r"\b(?:PAYNOW|PAYLAH|FAST\s+PAYMENT|FUNDS?\s+TRANSFER|TRANSFER\s+(?:TO|FROM)"
    r"|INCOMING|OUTGOING|TO:|FROM:|MR|MRS|MS|MDM)\b",
    re.IGNORECASE,
)
#: An account number, with or without its separators.
_ACCOUNT_LIKE = re.compile(r"\b\d[\d\s-]{5,}\d\b")


def gate_amount(amount_minor: int) -> int:
    """Snap an amount to the nearest gate, rounding up on a tie.

    Coarse enough that a figure carries a magnitude and not a purchase.
    """
    magnitude = abs(amount_minor)
    # Ties go to the larger gate, so the answer never understates.
    return min(AMOUNT_GATES, key=lambda gate: (abs(gate - magnitude), -gate))


def is_personal(counterparty: str) -> bool:
    """Whether a counterparty names a person or an account rather than a shop."""
    if not counterparty:
        return True
    return bool(_PERSONAL.search(counterparty) or _ACCOUNT_LIKE.search(counterparty))


@dataclass(frozen=True, slots=True)
class Proposal:
    """One distinct counterparty, as much as may leave the household about it.

    Deliberately not a transaction. The task is to name what a merchant *is*,
    which needs a vocabulary rather than a ledger, so nothing here carries a
    date, an account, or a row.
    """

    counterparty: str
    occurrences: int
    typical_amount_minor: int
    #: What this counterparty accounts for in total. Used to *order* the list
    #: and deliberately never sent: it is a real sum rather than a gated
    #: magnitude, and ordering is a decision made at home.
    total_value_minor: int = 0
    #: What the bundled or operator rules already think, if anything. Sent so a
    #: model reviews a proposal rather than answering from nothing — agreement
    #: is then evidence, and disagreement is a specific claim worth reading.
    suggested_category: str | None = None
    #: Where the suggestion came from, so it can be weighed rather than trusted.
    suggested_by: str | None = None


def propose(rows, rules=(), *, suggest=(), by: str = "value") -> list[Proposal]:
    """The counterparties worth asking about, aggregated and filtered.

    `rows` are `(counterparty, amount_minor)` pairs. `rules` are settled and
    remove a counterparty from the question entirely; `suggest` are the seed
    rules, which propose an answer without closing it.

    That split is the point. A seeded guess is a well-known chain matched by
    pattern, which is right often enough to be worth sending and wrong often
    enough that it should not be applied silently — so it travels as a claim to
    be confirmed, next to the evidence for it.

    Conduits and counterparties naming people never appear either way.
    """
    from .recurrence import is_conduit

    rules = RuleSet(rules)
    suggest = RuleSet(suggest)
    counts: dict[str, int] = {}
    amounts: dict[str, list[int]] = {}
    # Tallied first and judged once per distinct name, as in `review_queue`.
    for counterparty, amount_minor in rows:
        name = (counterparty or "").strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
        amounts.setdefault(name, []).append(abs(amount_minor))

    proposals = []
    for name, count in counts.items():
        if is_personal(name) or is_conduit(name) or categorise(name, rules).rule is not None:
            continue
        hint = categorise(name, suggest)
        proposals.append(Proposal(
            counterparty=name,
            occurrences=count,
            # The median, so one outlier purchase does not describe a merchant.
            typical_amount_minor=gate_amount(sorted(amounts[name])[len(amounts[name]) // 2]),
            total_value_minor=sum(amounts[name]),
            suggested_category=hint.category if hint.rule else None,
            suggested_by=hint.rule.pattern if hint.rule else None,
        ))

    # Ordered by money, not by frequency. Ranking on occurrences optimises the
    # row count and systematically ignores the large one-off spends, which is
    # where a household's money actually goes: on this corpus the top sixty by
    # value held 84% of everything still unaccounted for, while ordering by
    # count kept offering another bus fare.
    if by == "occurrences":
        return sorted(proposals, key=lambda p: (-p.occurrences, p.counterparty))
    return sorted(proposals, key=lambda p: (-p.total_value_minor, p.counterparty))


@dataclass(frozen=True, slots=True)
class Verdict:
    """What several models made of one counterparty."""

    counterparty: str
    category: str
    #: How many answered, and how many said this. Kept apart from confidence
    #: because a model's self-reported certainty and independent corroboration
    #: are different things, and only the second is evidence.
    agreed: int
    answered: int
    #: The categories that lost, where there were any.
    dissent: tuple[tuple[str, str], ...] = ()

    @property
    def is_unanimous(self) -> bool:
        return self.agreed == self.answered and self.answered > 0

    @property
    def needs_review(self) -> bool:
        return not self.is_unanimous


def consolidate(replies: dict, valid_categories=DEFAULT_CATEGORIES) -> list[Verdict]:
    """Reduce several models' answers to one verdict each.

    `replies` maps a model's name to `{counterparty: category}`.

    Consolidation is a vote, not an average. Averaging confidences would let
    one assured model outweigh two doubtful ones that happen to agree, and the
    whole reason for asking more than one is that **independent agreement is
    the only confidence signal available here** — a model's own certainty is
    not evidence about the world.

    A model that skipped a counterparty simply does not vote on it. Replies
    are routinely partial, and treating silence as dissent would make a short
    answer look like a disagreement.

    Categories outside the allowed set are discarded rather than accepted: a
    model inventing a label is answering a different question.
    """
    votes: dict[str, dict[str, list[str]]] = {}
    for model, answers in replies.items():
        for counterparty, category in (answers or {}).items():
            name = (counterparty or "").strip()
            if not name or category not in valid_categories:
                continue
            votes.setdefault(name, {}).setdefault(category, []).append(model)

    verdicts = []
    for name, by_category in votes.items():
        ranked = sorted(by_category.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        winner, backers = ranked[0]
        answered = sum(len(models) for models in by_category.values())
        verdicts.append(Verdict(
            counterparty=name,
            category=winner,
            agreed=len(backers),
            answered=answered,
            dissent=tuple(
                (category, ",".join(sorted(models)))
                for category, models in ranked[1:]
            ),
        ))
    return sorted(verdicts, key=lambda v: (v.needs_review, v.counterparty))


#: Weight given to a rule the operator wrote. Above anything imported or
#: seeded, so a decision made by hand is not quietly outvoted by a longer
#: pattern that shipped with the product.
OPERATOR_WEIGHT = 100

#: The note `operator_rule` stamps on what it writes. Kept as a constant
#: because `rule_origin` reads it back, and a decision that could not be told
#: apart from an import afterwards could not be offered back for removal.
OPERATOR_NOTE = "decided by operator"


def rule_origin(weight: int, note: str) -> str:
    """Who put a rule here — `operator` or `imported`.

    Both halves of the pair have to agree. Weight alone is not enough: an
    import is free to propose a weight, and a seed file is free to carry any
    note. Only what `operator_rule` writes is somebody's decision, and only a
    decision is a thing anybody should be invited to take back.
    """
    return "operator" if weight == OPERATOR_WEIGHT and note == OPERATOR_NOTE else "imported"


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """A counterparty nobody has placed, and what deciding it is worth."""

    counterparty: str
    occurrences: int
    total_minor: int
    #: What the models made of it, where they were asked. `None` means never
    #: asked; `Others` means asked and unanimously declined.
    model_verdict: str | None = None
    #: True where the models were asked and disagreed, which is a different
    #: kind of unknown from nobody recognising the name.
    disputed: bool = False


def review_queue(rows, rules, *, verdicts=None) -> list[ReviewItem]:
    """What still needs a person, ranked by what answering it is worth.

    `rows` are `(counterparty, amount_minor)`. Ordered by value rather than by
    frequency for the reason the whole categorisation effort is: on this corpus
    the largest sixty unplaced names carried 88% of the unaccounted money, and
    ranking by count offers a long queue of bus fares first.

    Conduits and personal counterparties are excluded, as everywhere else —
    they are not spending anyone needs to classify.
    """
    from .recurrence import is_conduit

    verdicts = verdicts or {}
    rules = RuleSet(rules)
    counts: dict[str, int] = {}
    totals: dict[str, int] = {}

    # Tally first, judge second. The three predicates below are pure functions
    # of the name, and `categorise` runs every rule against it — on this corpus
    # that was 536 rules over 3,620 rows, nearly two million regex matches, for
    # roughly nine hundred distinct names. Judging the names instead of the rows
    # is the same answer for a quarter of the work.
    for counterparty, amount_minor in rows:
        name = (counterparty or "").strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
        totals[name] = totals.get(name, 0) + abs(amount_minor)

    items = [
        ReviewItem(
            counterparty=name,
            occurrences=count,
            total_minor=totals[name],
            model_verdict=getattr(verdicts.get(name), "category", None),
            disputed=bool(getattr(verdicts.get(name), "needs_review", False)),
        )
        for name, count in counts.items()
        # Conduits and personal counterparties are excluded, as everywhere
        # else — they are not spending anyone needs to classify.
        if not is_personal(name) and not is_conduit(name)
        and categorise(name, rules).rule is None
    ]
    return sorted(items, key=lambda i: (-i.total_minor, i.counterparty))


def operator_rule(counterparty: str, category: str) -> tuple[str, str, int, str]:
    """A decision about one counterparty, as a rule.

    Anchored and escaped, because a decision is about *this* name and a loose
    pattern would claim every other name containing it. Weighted above
    everything imported so that having decided something by hand is the end of
    the argument rather than another vote in it.
    """
    import re as _re

    return (rf"^{_re.escape(counterparty)}$", category, OPERATOR_WEIGHT, OPERATOR_NOTE)


def coverage(counterparties, rules) -> dict:
    """How much of a ledger the rules actually account for.

    Reported by row rather than by distinct name, because a rule catching one
    merchant seen four hundred times is worth four hundred rules catching one
    each — and the point of stage one is volume.
    """
    decided = contested = 0
    unmatched: dict[str, int] = {}

    # Counted by row, decided by name. `categorise` is a pure function of the
    # two, and a household's ledger names the same shop hundreds of times.
    rules = RuleSet(rules)
    seen: dict[str, Decision] = {}

    for counterparty in counterparties:
        decision = seen.get(counterparty)
        if decision is None:
            decision = seen[counterparty] = categorise(counterparty, rules)
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
