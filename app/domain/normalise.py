"""Description normalisation.

`description_norm` is what dedupe keys, rules and later k-NN all key on, so it
has to be stable: the same transaction described the same way must normalise
identically every time, across parser versions where possible.
"""

from __future__ import annotations

import re
import unicodedata

# Trailing acquirer noise that varies between statements for the same merchant.
_TRAILING_COUNTRY = re.compile(r"\s+(?:SG|SGP|SINGAPORE|MY|US|USA|GB|UK|AU|NZ|HK|JP|IN|ID|TH|VN|PH|CN)\s*$")
_CARD_TAIL = re.compile(r"\s*\b(?:\d{4}[- ]?){0,3}\d{4}\b\s*$")
_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s&/.*'-]")


def normalise_description(raw: str) -> str:
    """Collapse a raw statement description to its stable form.

    Case, accents, punctuation noise and repeated whitespace are removed; the
    merchant text itself is kept intact so it stays human-readable in the
    review queue.
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace(" ", " ")
    text = _WHITESPACE.sub(" ", text).strip()
    text = _PUNCT.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text.upper()


#: What the bank calls the *mechanism*, printed before or after the party it
#: was done with. Every one of these is an institution's own vocabulary, taken
#: from the corpus rather than guessed: DBS leads with the type and trails with
#: a reference, MariBank appends the type underneath the row.
#:
#: Stripping them is what makes "GIRO ACME LIFE LTD AVI1234567" and
#: "ACME LIFE LTD" the same counterparty. Left in, one merchant paid three
#: ways looks like three merchants and nothing recurs.
_MECHANISM = re.compile(
    r"\b(?:"
    r"FAST PAYMENT|FAST TRANSFER|ADVICE|STANDING INSTRUCTION|FUNDS TRANSFER"
    r"|PAYMENTS?|COLLECTIONS?|VIA GIRO|GIRO|BILL PAYMENT|CASH WITHDRAWAL"
    r"|CASHCARD|FLASHPAY|TOP-?UP|EZ-?LINK CARD|QUICK CHEQUE DEPOSIT"
    r"|CARD PAYMENT|INSTANT CHECKOUT|PAYNOW TRANSFER|PAYNOW|INCOMING|OUTGOING"
    r"|RECEIPT|TRANSFER|DEBIT|CREDIT|I-BANK|IBANKING|NETS|MST|POS"
    r"|REF|FROM|TO|OTHER"
    r")\b"
)

#: A reference rather than a name: anything carrying a run of digits long
#: enough that it cannot be part of a merchant's identity. Four is the shortest
#: safe threshold — "SS 4A" is a store, "100200300" is a terminal.
_REFERENCE = re.compile(r"\b[A-Z]*\d{4,}[A-Z0-9]*\b")

#: Separators left stranded once the words around them are gone.
_STRANDED = re.compile(r"(?:^|(?<= ))[/&.*'-]+(?= |$)")


def normalise_counterparty(raw: str) -> str:
    """Best-effort merchant identity, with the bank's decoration stripped.

    `description_norm` deliberately keeps everything, because it feeds the
    dedupe key and that must never shift under a ledger. This is the other
    half: the same row reduced to *who* it was with, free to be improved
    whenever the corpus shows something new, because nothing keyed on identity
    depends on it.

    That freedom is the whole design. Recurrence and categorisation both group
    on this, and both get better as it does, without a single dedupe key
    changing — a reparse updates the column and every transaction keeps the
    identity it was imported with.
    """
    text = normalise_description(raw)
    if not text:
        return ""

    text = _REFERENCE.sub(" ", text)
    text = _MECHANISM.sub(" ", text)
    text = _STRANDED.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()

    previous = None
    while previous != text:
        previous = text
        text = _TRAILING_COUNTRY.sub("", text).strip()
        text = _CARD_TAIL.sub("", text).strip()

    # Everything stripped means the row was mechanism and reference only — a
    # transfer with no named party. Falling back keeps it distinguishable
    # rather than collapsing every such row into one empty merchant.
    return text or normalise_description(raw)
