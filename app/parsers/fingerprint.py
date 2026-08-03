"""Layout identification.

Two things live here, and they do different jobs:

- **`LayoutSignature` routes documents to adapters.** An adapter declares the
  header lines that identify its format; a document matches when it contains
  all of them.
- **`fingerprint_pdf` records what a document looked like.** It is stored on
  `source_document` and printed in failure reports, so a layout can be talked
  about precisely. It no longer decides anything.

Routing used to be the fingerprint, compared exactly. That was too brittle,
for reasons found in production rather than anticipated — see
`LayoutSignature` — and, worse, it made routing depend on the customer's name
and address.

A fingerprint identifies a *layout*, not a document. Two statements from the
same institution in different months should fingerprint identically; a
statement whose layout has actually changed should not.

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

The producer string is included but **stripped of digits first**: Trust renders
through headless Chromium, and a browser upgrade moved the producer from
"Skia/PDF m80" to "Skia/PDF m141", which was enough to make an unchanged
statement quarantine as a new layout. The tool is a useful signal; its version
is not.

Caveat worth knowing: a genuinely new digit-free header line — a new marketing
strapline, say — produces a new fingerprint, which quarantines rather than
importing. That is the intended loud, boring failure. Registering the new
fingerprint against the existing adapter is a one-line change, and the registry
maps many fingerprints onto one adapter for exactly this reason.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .pdfio import Document, Page

_DIGIT = re.compile(r"\d")
_PUNCT = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")

#: Fingerprint algorithm version. Bumping this invalidates every registered
#: fingerprint, so it changes only when the algorithm itself does.
ALGORITHM_VERSION = "3"

#: Fraction of page 1 treated as the header band. Statement headers sit above
#: the summary and transaction tables on every layout in the corpus; see the
#: module docstring for why this bound exists.
HEADER_BAND_FRACTION = 0.35


def normalise_label(text: str) -> str:
    text = text.lower().replace("’", "'").replace(" ", " ")
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def normalise_producer(text: str) -> str:
    """Drop the version from a producer or creator string.

    The tool that rendered a PDF is a useful layout signal; the *version* of
    that tool is not. Trust renders statements through headless Chromium, so
    the producer moved from "Skia/PDF m80" to "Skia/PDF m141" when they
    upgraded — a browser upgrade, not a layout change, but enough to make an
    otherwise identical statement fingerprint as a new layout and quarantine.

    Stripping digits keeps the discriminating part ("Skia/PDF", "Streamline
    PDFGen for OCBC Group") and discards the volatile part.
    """
    return _WS.sub(" ", re.sub(r"[\d.]+", "", text or "")).strip().lower()


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
        normalise_producer(document.producer),
        normalise_producer(document.creator),
        f"{round(first.width)}x{round(first.height)}",
        "\x1e".join(label_lines(first)),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


#: Page dimensions this far apart are the same paper size. Chromium's renderer
#: moved A4 from 595x842 to 596x843 between versions.
PAGE_TOLERANCE = 10.0


@dataclass(frozen=True, slots=True)
class LayoutSignature:
    """What an adapter asserts a document of its layout must contain.

    Routing used to be an exact hash of the header band, which broke twice in
    production for reasons that had nothing to do with the layout:

    - the producer version moved with a browser upgrade;
    - the customer's address changed, and a street name with no digits in it
      passes the digit filter that excludes every other piece of personal data.

    That second one is the important one. Under exact matching, **every**
    fingerprint in the corpus included the customer's name, so layout identity
    depended on who the customer was and where they lived — personal data
    determining routing, and stored in the ledger.

    A signature instead names the handful of lines that identify the *format*:
    the bank's own name, the statement's own title. A document matches when it
    contains all of them. Extra lines — an address, a new marketing strapline,
    a name — are ignored, because they were never evidence of the layout.

    This is not guessing. The required lines are asserted deliberately by
    whoever writes the adapter, an ambiguous match is an error rather than a
    coin toss, and the balance check remains the backstop that makes a wrong
    match loud instead of silent.
    """

    #: Normalised producer. Empty means "do not test", for issuers that ship no
    #: metadata at all.
    producer: str = ""
    #: Normalised creator. Some issuers leave Producer empty and put the
    #: rendering tool in Creator instead — DBS ships
    #: "Quadient Group AG~Inspire~12.5.33.0" with no producer at all.
    creator: str = ""
    #: Header lines that must all be present, already normalised.
    requires: tuple[str, ...] = ()
    #: Expected page size, compared within PAGE_TOLERANCE. None means any.
    page_size: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if not self.requires:
            raise ValueError("a layout signature must require at least one header line")

    def matches(self, document: Document) -> bool:
        if not document.pages:
            return False
        first = document.pages[0]

        if self.producer and normalise_producer(document.producer) != self.producer:
            return False

        if self.creator and normalise_producer(document.creator) != self.creator:
            return False

        if self.page_size is not None:
            width, height = self.page_size
            if abs(first.width - width) > PAGE_TOLERANCE or abs(first.height - height) > PAGE_TOLERANCE:
                return False

        return set(self.requires).issubset(set(label_lines(first)))

    def missing_from(self, document: Document) -> list[str]:
        """Which required lines a document lacks — the useful half of a failure."""
        if not document.pages:
            return list(self.requires)
        present = set(label_lines(document.pages[0]))
        return [line for line in self.requires if line not in present]


def describe(document: Document) -> dict:
    """Human-facing detail for `finstone fingerprint` and quarantine reports."""
    first = document.pages[0] if document.pages else None
    return {
        "fingerprint": fingerprint_pdf(document) if first else None,
        "algorithm_version": ALGORITHM_VERSION,
        "producer": document.producer,
        "creator": document.creator,
        "producer_normalised": normalise_producer(document.producer),
        "pages": len(document.pages),
        "page_size": f"{round(first.width)}x{round(first.height)}" if first else None,
        "label_lines": label_lines(first) if first else [],
    }
