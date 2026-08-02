"""Layout fingerprinting.

A fingerprint identifies a *layout*, not a document. Two statements from the
same institution in different months must fingerprint identically; a statement
whose layout has actually changed must not.

Architecture §2.3 suggests hashing page-1 header text, column x-positions and
producer metadata. Column positions turned out to be unusable for this corpus:
Trust savings statements render one table row per savings pocket, so the x
geometry shifts whenever a pocket is added or removed — the layout is
unchanged but the positions are not. Raw page-1 text is worse still, since it
contains the balances themselves.

What is stable is the set of **digit-free lines in the header band of page 1**.
Those are almost exactly the layout's static labels — "Credit card statement",
"Trust Bank Singapore Limited", "YOUR SAVINGS ACCOUNT BY TRUST STATEMENT IS
READY" — while every line carrying data (amounts, dates, account numbers,
addresses, page numbers) is excluded automatically, because data has digits in
it.

Both restrictions were arrived at by measuring against the real corpus, not by
taste:

- Without the digit filter, every month is a new fingerprint.
- Without the header band, statements whose transaction descriptions happen to
  render on their own line leak those descriptions into the label set, and
  every month is a new fingerprint again. Over the 11-document corpus, banding
  to the top 35% took Trust card statements from 2 fingerprints to 1 and
  MariBank savings from 2 to 1, with no collisions between institutions.

Caveat worth knowing: a genuinely new digit-free header line — a new marketing
strapline, say — produces a new fingerprint, which quarantines rather than
importing. That is the intended loud, boring failure. Registering the new
fingerprint against the existing adapter is a one-line change, and the registry
maps many fingerprints onto one adapter for exactly this reason.
"""

from __future__ import annotations

import hashlib
import re

from .pdfio import Document, Page

_DIGIT = re.compile(r"\d")
_PUNCT = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")

#: Fingerprint algorithm version. Bumping this invalidates every registered
#: fingerprint, so it changes only when the algorithm itself does.
ALGORITHM_VERSION = "2"

#: Fraction of page 1 treated as the header band. Statement headers sit above
#: the summary and transaction tables on every layout in the corpus; see the
#: module docstring for why this bound exists.
HEADER_BAND_FRACTION = 0.35


def normalise_label(text: str) -> str:
    text = text.lower().replace("’", "'").replace(" ", " ")
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def label_lines(page: Page, band: float = HEADER_BAND_FRACTION) -> list[str]:
    """The digit-free header lines of a page, normalised, deduped and sorted.

    Sorted because line order can shift with content length while the layout
    stays the same.
    """
    cutoff = page.height * band
    labels = set()
    for line in page.lines:
        if line.top > cutoff or _DIGIT.search(line.text):
            continue
        label = normalise_label(line.text)
        if len(label) >= 3:
            labels.add(label)
    return sorted(labels)


def fingerprint_pdf(document: Document) -> str:
    """Compute the layout fingerprint of a parsed PDF."""
    if not document.pages:
        raise ValueError("cannot fingerprint a PDF with no pages")
    first = document.pages[0]
    payload = "\x1f".join([
        ALGORITHM_VERSION,
        document.producer,
        document.creator,
        f"{round(first.width)}x{round(first.height)}",
        "\x1e".join(label_lines(first)),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def describe(document: Document) -> dict:
    """Human-facing detail for `finstone fingerprint` and quarantine reports."""
    first = document.pages[0] if document.pages else None
    return {
        "fingerprint": fingerprint_pdf(document) if first else None,
        "algorithm_version": ALGORITHM_VERSION,
        "producer": document.producer,
        "creator": document.creator,
        "pages": len(document.pages),
        "page_size": f"{round(first.width)}x{round(first.height)}" if first else None,
        "label_lines": label_lines(first) if first else [],
    }
