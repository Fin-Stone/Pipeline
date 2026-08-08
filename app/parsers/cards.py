"""Card numbers, and the only part of one this ledger keeps.

A card account is keyed by its *product* — "Ocbc Rewards Card" — and never by
its number, because a number changes on reissue while the account continues,
and keying on it would fork one card's history in two.

That is the right identity and it leaves a hole. A deposit statement records
paying the bill against the **number**, not the product: DBS writes
`Advice Bill Payment CCC - <sixteen digits>`, and nothing in the ledger
connects those digits to the card they settle. The payment is then either
unmatched — money leaving the household for nowhere — or matched on amount
alone, which cannot tell two cards apart when both are paid in the same month.

So the number is kept, **masked to its last four**, which is all the matching
needs and all a ledger has any business holding. Kept as a set that grows
rather than a field that is overwritten: the point is precisely that a card
has been several numbers over its life, and a payment to the number it carried
three years ago still settled this card.

Statements write the number three ways and all three appear in this corpus:
grouped (`4111 1111 1111 4321`), solid, and already masked by the bank
(`************8765`, `****2468`). One reader, because which one a bank chose
is not a fact any adapter should have to hold an opinion about.
"""

from __future__ import annotations

import re

#: Sixteen characters in four groups, where the last four must be real digits —
#: the rest may already be masked by the bank. The lookarounds stop a longer
#: run of digits, such as a reference number, from yielding its tail.
_GROUPED = re.compile(r"(?<![\dXx*])(?:[\dXx*]{4}[ -]?){3}(\d{4})(?![\dXx*])")

#: The shorter form, where the bank prints only the tail behind a run of mask
#: characters. Deliberately not applied to a whole document — four digits after
#: some asterisks is too little to be sure of on its own, so adapters use this
#: where they already know they are looking at a card.
_MASKED_TAIL = re.compile(r"[Xx*]{4,}[ -]?(\d{4})(?!\d)")

#: How a card number is written once it is in the ledger. The same shape HSBC's
#: adapter already used for an account reference, so a card whose product is
#: unknown and a card whose number is known read alike.
MASK = "xxxx-xxxx-xxxx-{}"


def masked(last_four: str) -> str:
    """A card number as this ledger stores it."""
    return MASK.format(last_four)


def find(text: str) -> str | None:
    """The first card number in a line, masked. `None` if there is none."""
    for pattern in (_GROUPED, _MASKED_TAIL):
        found = pattern.search(text or "")
        if found:
            return masked(found.group(1))
    return None


def find_all(lines) -> tuple[str, ...]:
    """Every card number across some lines, masked, in the order they appear.

    De-duplicated, because a statement prints the number in its header and
    again beside the payment slip, and that is one card rather than two.
    """
    seen: dict[str, None] = {}
    for line in lines:
        found = find(getattr(line, "text", line))
        if found:
            seen.setdefault(found, None)
    return tuple(seen)


__all__ = ["MASK", "find", "find_all", "masked"]
