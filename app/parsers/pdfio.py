"""The only module that touches pdfplumber.

Adapters build on the line model here rather than each re-deriving how to turn
a bag of positioned words into rows. Two things it handles that every statement
adapter would otherwise get wrong:

- **Vertical tolerance.** Statement rows are not perfectly aligned. Trust puts
  a row's description at one baseline and its amount two points lower, so
  grouping by exact `top` splits a single transaction across two lines.
- **Column extraction by x-position.** Amounts are right-aligned in their
  column, so "which column is this number in" is a question about x, not about
  word order.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pdfplumber

from . import glyphs

#: Words whose baselines are within this many points belong to the same row.
DEFAULT_LINE_TOLERANCE = 4.0


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float

    @property
    def centre(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass(frozen=True, slots=True)
class Line:
    """One visual row of a page.

    Carries its page and vertical position so that a failure can name exactly
    where in the document it happened — the difference between a diagnostic an
    operator can act on and one that requires sending the statement.
    """

    top: float
    words: tuple[Word, ...]
    page_number: int = 0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def location(self) -> str:
        return f"page {self.page_number}, y={self.top:.0f}"

    def words_between(self, x_min: float, x_max: float) -> tuple[Word, ...]:
        """Words whose left edge falls inside a column band."""
        return tuple(w for w in self.words if x_min <= w.x0 < x_max)

    def text_between(self, x_min: float, x_max: float) -> str:
        return " ".join(w.text for w in self.words_between(x_min, x_max))

    def starts_with(self, prefix: str) -> bool:
        return self.text.lower().startswith(prefix.lower())

    def contains(self, needle: str) -> bool:
        return needle.lower() in self.text.lower()


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    width: float
    height: float
    lines: tuple[Line, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True, slots=True)
class Document:
    path: Path
    metadata: dict
    pages: tuple[Page, ...]

    @property
    def producer(self) -> str:
        return str(self.metadata.get("Producer") or "")

    @property
    def creator(self) -> str:
        return str(self.metadata.get("Creator") or "")

    def lines(self):
        """Every line in the document, in reading order across pages."""
        for page in self.pages:
            yield from page.lines

    def find_line(self, needle: str) -> Line | None:
        for line in self.lines():
            if line.contains(needle):
                return line
        return None


class PdfOpenError(Exception):
    """Raised when a PDF cannot be opened — including a wrong or missing password."""


def group_words(words, tolerance: float = DEFAULT_LINE_TOLERANCE, page_number: int = 0) -> tuple[Line, ...]:
    """Cluster positioned words into visual rows.

    Words are sorted by baseline and accumulated while they stay within
    `tolerance` of the row's first baseline. Using the row's anchor rather
    than the previous word stops a column of slightly-drifting baselines from
    merging a whole page into one row.
    """
    items = sorted(
        (Word(w["text"], float(w["x0"]), float(w["x1"]), float(w["top"])) for w in words),
        key=lambda w: (w.top, w.x0),
    )
    lines: list[Line] = []
    bucket: list[Word] = []
    anchor: float | None = None
    for word in items:
        if anchor is None or abs(word.top - anchor) <= tolerance:
            if anchor is None:
                anchor = word.top
            bucket.append(word)
        else:
            lines.append(Line(anchor, tuple(sorted(bucket, key=lambda w: w.x0)), page_number))
            bucket = [word]
            anchor = word.top
    if bucket and anchor is not None:
        lines.append(Line(anchor, tuple(sorted(bucket, key=lambda w: w.x0)), page_number))
    return tuple(lines)


def load(path: Path, *, password: str | None = None, tolerance: float = DEFAULT_LINE_TOLERANCE) -> Document:
    """Open a PDF and return its positioned text.

    An empty password is tried first and covers the common case: several
    institutions ship owner-restricted statements that open without any
    credential at all. A configured password is used when the empty one fails.
    """
    attempts = ["", password] if password else [""]
    last_error: Exception | None = None
    for attempt in attempts:
        try:
            return _load_with(path, attempt, tolerance)
        except Exception as exc:  # pdfminer raises several distinct types here
            last_error = exc
    raise PdfOpenError(f"cannot open {path.name}: {last_error}") from last_error


def _load_with(path: Path, password: str, tolerance: float) -> Document:
    with pdfplumber.open(path, password=password) as pdf:
        pages = []
        for index, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            if not words and glyphs.page_needs_glyphs(page):
                # A page with no characters and thousands of one-bit pictures
                # is a page whose text was *drawn*. Reconstructed here rather
                # than in an adapter, so every adapter, the fingerprinter and
                # the diagnostics stay unaware it happened. See glyphs.py.
                words = glyphs.read_page(page)
            pages.append(Page(index, float(page.width), float(page.height), group_words(words, tolerance, index)))
        return Document(path=path, metadata=dict(pdf.metadata or {}), pages=tuple(pages))
