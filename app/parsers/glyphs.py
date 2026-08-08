"""Reading a PDF that has no text in it.

Some statement generators — OpenText's Output Transformation Engine is the one
in this corpus — emit every character as a separate 1-bit bitmap image and
embed no fonts at all. `pdfplumber` and `pypdf` both extract zero characters,
because there genuinely are none: an HSBC card statement is 8,862 tiny pictures
arranged to look like words.

**This is not OCR.** A generator draws the same character from the same bitmap
every time, so 17,007 glyph instances across two statements come from only 472
distinct images. Recognising them is therefore a dictionary lookup on the
bitmap's hash, not a guess about its shape — exact, deterministic, and unable
to mistake an 8 for a 3 in an amount. The table was built once by matching each
distinct bitmap against rendered system fonts and checking the result against
the statements' own arithmetic.

What the table cannot carry is **case**, for the letters whose capital is the
same shape as the small form — c, o, s, u, v, w, x, z. Nothing about the
picture of an `o` says which it is; only its size relative to its neighbours
does, and that is a property of the line it sits on rather than of the glyph.
So the table stores those lowercase and `_resolve_case` restores the capitals
from the height of the line's own letters.

Where a residual error would matter it cannot occur: counterparties are
upper-cased by normalisation, month names and `CR` markers are read
case-insensitively, and digits and decimal points have no case at all. The
balance check is what proves it — a statement whose amounts were misread does
not reconcile, and these do, to the cent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: Where the alphabet lives. Data rather than code: it is a few hundred opaque
#: hashes, and burying that in a module would make the module unreadable
#: without making the table any easier to check.
TABLE_DIR = Path(__file__).parent / "glyph_tables"

#: Below this, a page is treated as having no usable text of its own. Not zero:
#: a generator may emit a handful of real characters — a page number, a footer —
#: alongside thousands of bitmaps, and a page that is 99% pictures is a page
#: with no text whatever the last few characters say.
MIN_REAL_CHARS = 24

#: And this many glyph images before it is worth trying. A statement page runs
#: to thousands; a scanned page with one logo on it is not what this is for.
MIN_GLYPHS = 200

#: Letters whose capital and small forms differ only in size. Stored lowercase
#: in the table and resolved per line.
CASE_AMBIGUOUS = frozenset("cosuvwxz")

#: How much taller than its line's small letters a glyph must be to be read as
#: a capital. Cap height is about 1.4x x-height across the fonts in this
#: corpus, so the midpoint separates them with room on both sides.
CAP_RATIO = 1.18

#: A gap wider than this fraction of the line's median glyph *width* is a
#: space.
#:
#: Width, not height. The gap being measured is between the inked edges of two
#: bitmaps, and how much blank an advance leaves is a property of the character
#: widths, not of how tall they are. Measured across this corpus: gaps inside a
#: word reach 0.33 of the median width, gaps between words start at 0.48. A
#: height-based threshold sat between 1.5 and 2.0 points and split `188.81`
#: into `1 88.81` — because a `1` is narrow and leaves as much blank after it
#: as a real space does.
SPACE_GAP = 0.40


@dataclass(frozen=True, slots=True)
class Glyph:
    """One drawn character, with where it sits on the page."""

    char: str
    x0: float
    x1: float
    top: float
    bottom: float
    #: Bitmap height in pixels, which is the only clue to the font size.
    height: int


class GlyphTable:
    """Bitmap digest to character."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping

    def __len__(self) -> int:
        return len(self._mapping)

    def get(self, digest: str) -> str | None:
        return self._mapping.get(digest)


@lru_cache(maxsize=None)
def load_tables() -> GlyphTable:
    """Every known alphabet, merged.

    Merged rather than selected by producer: a digest identifies a bitmap
    exactly, so two generators sharing a font share entries harmlessly, and a
    collision between different characters is not a thing a SHA-1 of the pixels
    does by accident.
    """
    merged: dict[str, str] = {}
    if TABLE_DIR.is_dir():
        for path in sorted(TABLE_DIR.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            merged.update(data.get("glyphs", {}))
    return GlyphTable(merged)


def digest_of(raw: bytes, width: int, height: int) -> str:
    """The key for one glyph bitmap.

    Size is folded in because the same pixel run at two different widths is two
    different pictures, and a bare content hash would conflate them.
    """
    return hashlib.sha1(f"{width}x{height}".encode() + raw).hexdigest()[:16]


def page_needs_glyphs(page) -> bool:
    """Whether this page's text has to be reconstructed from pictures."""
    if len(page.chars) >= MIN_REAL_CHARS:
        return False
    return sum(1 for image in page.images if image.get("bits") == 1) >= MIN_GLYPHS


def read_page(page, table: GlyphTable | None = None) -> list[dict]:
    """Words for a page whose characters are drawn rather than typed.

    Returns the same shape `pdfplumber.extract_words` does, so everything
    downstream — line grouping, column extraction, every adapter — is unaware
    this happened.
    """
    table = table or load_tables()
    glyphs = _extract(page, table)
    if not glyphs:
        return []

    words: list[dict] = []
    for line in _lines(glyphs):
        words.extend(_words(_resolve_case(line)))
    return words


def _extract(page, table: GlyphTable) -> list[Glyph]:
    found: list[Glyph] = []
    for image in page.images:
        if image.get("bits") != 1:
            continue
        width, height = image.get("srcsize") or (0, 0)
        if not width or not height:
            continue
        try:
            raw = image["stream"].get_data()
        except Exception:
            # A stream that will not decode is a picture nobody can read, not
            # a reason to fail the document. The balance check still has to
            # pass, so anything genuinely lost here is caught downstream.
            continue
        if len(raw) < ((width + 7) // 8) * height:
            continue
        char = table.get(digest_of(raw, width, height))
        if char is None:
            # A rule, a logo, or a glyph nobody has labelled. Dropped rather
            # than guessed at: an unknown character invented here would be
            # indistinguishable from a real one downstream.
            continue
        found.append(Glyph(
            char=char,
            x0=float(image["x0"]), x1=float(image["x1"]),
            top=float(image["top"]), bottom=float(image["bottom"]),
            height=int(height),
        ))
    return found


def _lines(glyphs: list[Glyph]) -> list[list[Glyph]]:
    """Group glyphs into the rows they were drawn on.

    Clustered on the baseline, which every glyph on a line shares — but a
    descender hangs below it, so a naive grouping on the bottom edge tore
    "update" into "u date" on one row and a lone "p" on the next.

    So the baselines are found from the crowd first: the most populated bottom
    edges, spaced apart, are where the lines are. Each glyph then joins the
    nearest one within half its own height, which is ample for a descender and
    far short of the next line.
    """
    if not glyphs:
        return []

    counts: dict[float, int] = {}
    for glyph in glyphs:
        counts[round(glyph.bottom, 1)] = counts.get(round(glyph.bottom, 1), 0) + 1

    baselines: list[float] = []
    for value in sorted(counts, key=lambda v: (-counts[v], v)):
        if all(abs(value - taken) > 2.0 for taken in baselines):
            baselines.append(value)
    baselines.sort()

    rows: dict[float, list[Glyph]] = {}
    for glyph in glyphs:
        nearest = min(baselines, key=lambda b: abs(glyph.bottom - b))
        key = nearest if abs(glyph.bottom - nearest) <= max(2.0, glyph.height * 0.5) \
            else round(glyph.bottom, 1)
        rows.setdefault(key, []).append(glyph)

    return [sorted(row, key=lambda g: g.x0) for _, row in sorted(rows.items())]


def _resolve_case(line: list[Glyph]) -> list[Glyph]:
    """Restore the capitals the table could not carry.

    A line is set in one font size, which is the only context in which "taller
    than its neighbours" means anything. The small letters on the line set the
    x-height; an ambiguous glyph meaningfully above it is a capital.

    Falls back to leaving them lowercase where a line is too short to establish
    an x-height — a two-glyph line offers no evidence, and inventing some would
    be worse than the small cosmetic error of getting it wrong.
    """
    heights = sorted(g.height for g in line if g.char.isalpha())
    if len(heights) < 4:
        return line
    x_height = heights[len(heights) // 2]

    return [
        Glyph(
            char=g.char.upper() if (
                g.char in CASE_AMBIGUOUS and g.height >= x_height * CAP_RATIO
            ) else g.char,
            x0=g.x0, x1=g.x1, top=g.top, bottom=g.bottom, height=g.height,
        )
        for g in line
    ]


def _words(line: list[Glyph]) -> list[dict]:
    """Split a row of glyphs into words on the gaps between them.

    The threshold scales with the line's own glyph width rather than being a
    fixed number of points: the same statement sets its table in 6pt and its
    headings in 12pt, and one absolute gap either runs the headings together or
    splits the table's own figures apart.
    """
    if not line:
        return []

    widths = sorted(g.x1 - g.x0 for g in line)
    typical = widths[len(widths) // 2] or 1.0
    space_gap = SPACE_GAP * typical

    words: list[dict] = []
    buffer: list[Glyph] = []

    def flush() -> None:
        if buffer:
            words.append({
                "text": "".join(g.char for g in buffer),
                "x0": buffer[0].x0,
                "x1": buffer[-1].x1,
                "top": min(g.top for g in buffer),
                "bottom": max(g.bottom for g in buffer),
            })
            buffer.clear()

    previous: Glyph | None = None
    for glyph in line:
        if previous is not None and glyph.x0 - previous.x1 > space_gap:
            flush()
        previous = glyph
        buffer.append(glyph)
    flush()

    # Column boundaries need no special handling: they are simply very wide
    # gaps, and the x-positions on each word already carry them for
    # `words_between` to find.
    return [w for w in words if w["text"].strip()]


__all__ = [
    "GlyphTable", "Glyph", "load_tables", "page_needs_glyphs", "read_page",
    "digest_of", "SPACE_GAP",
]
