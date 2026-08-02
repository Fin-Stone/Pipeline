"""Minimal PDF writer for test fixtures.

Deliberately dependency-free: writes uncompressed PDFs with a text layer from a
list of (x, y, text) placements. The suite needs deterministic documents it can
commit and reason about, and pulling in a PDF *generation* library to test a
PDF *parsing* library would be a poor trade.

Only what pdfplumber needs to extract positioned words is emitted: one page,
one built-in font, absolute text positioning.
"""

from __future__ import annotations

from pathlib import Path

PAGE_WIDTH = 595
PAGE_HEIGHT = 842


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(placements: list[tuple[float, float, str]], *, font_size: float = 9.0,
              width: float = PAGE_WIDTH, height: float = PAGE_HEIGHT) -> bytes:
    """Render placements given in top-left coordinates, as pdfplumber reports them."""
    parts = ["BT", f"/F1 {font_size} Tf"]
    for x, top, text in placements:
        y = height - top - font_size
        parts.append(f"1 0 0 1 {x:.2f} {y:.2f} Tm ({_escape(text)}) Tj")
    parts.append("ET")
    stream = "\n".join(parts).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
         f"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>").encode("latin-1"),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def write_pdf(path: Path, placements: list[tuple[float, float, str]], **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_pdf(placements, **kwargs))
    return path


# --- A synthetic statement layout the tests can drive -----------------------
# Mirrors the shape of a real statement (header band, transaction table with a
# previous/closing balance) without imitating any institution's document.

HEADER_X, DESC_X, FCY_X, SGD_X = 53.0, 146.0, 367.0, 475.0


def synthetic_statement(
    rows: list[tuple[str, str, str]],
    *,
    opening: str = "1,000.00",
    closing: str | None = None,
    period: str = "1 Jun 2024 - 30 Jun 2024",
    account_ref: str = "01-1234567-8",
    pocket: str = "Main Account",
    strapline: str = "SYNTHETIC TEST STATEMENT",
) -> list[tuple[float, float, str]]:
    """Build placements for a one-pocket statement.

    `rows` are (date, description, amount) with the amount written exactly as
    a statement would: a leading "+" for credits, bare for debits.
    """
    placements: list[tuple[float, float, str]] = [
        (399.0, 64.0, "Test Bank Placeholder Limited"),
        (HEADER_X, 136.0, "TEST CUSTOMER"),
        (302.0, 136.0, "Account Statement"),
        (460.0, 136.0, account_ref),
        (302.0, 156.0, "Statement period"),
        (449.0, 156.0, period),
        (302.0, 168.0, "Statement date"),
        (496.0, 168.0, "3 Jul 2024"),
        (HEADER_X, 250.0, strapline),
        (HEADER_X, 128.0 + 800.0, ""),  # keeps the page tall enough
    ]

    top = 400.0
    placements.append((HEADER_X, top, "TRANSACTION DETAILS"))
    top += 38.0
    placements.append((HEADER_X, top, pocket))
    top += 28.0
    for x, label in ((HEADER_X, "Posting date"), (DESC_X, "Description"),
                     (FCY_X, "Amount in FCY"), (SGD_X, "Amount in SGD")):
        placements.append((x, top, label))
    top += 28.0

    placements += [(HEADER_X, top, "01 Jun"), (DESC_X, top, "Previous balance"), (SGD_X + 22, top, opening)]
    top += 28.0

    for date_text, description, amount in rows:
        placements.append((HEADER_X, top, date_text))
        placements.append((DESC_X, top, description))
        placements.append((SGD_X + 22, top, amount))
        top += 28.0

    if closing is not None:
        placements += [(HEADER_X, top, "30 Jun"), (DESC_X, top, "Closing balance"), (SGD_X + 22, top, closing)]

    return placements
