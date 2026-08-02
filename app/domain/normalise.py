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


def normalise_counterparty(raw: str) -> str:
    """Best-effort merchant identity, with acquirer decoration stripped.

    Deliberately conservative: it removes only the trailing country code and a
    trailing card/reference number, both of which are known to vary for the
    same merchant. Anything cleverer belongs in the enrichment phase, where a
    wrong guess is a correctable label rather than part of a dedupe key.
    """
    text = normalise_description(raw)
    if not text:
        return ""
    previous = None
    while previous != text:
        previous = text
        text = _TRAILING_COUNTRY.sub("", text).strip()
        text = _CARD_TAIL.sub("", text).strip()
    return text
