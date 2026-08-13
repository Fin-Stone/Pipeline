"""Reading a statement's transaction table.

Every institution in the corpus prints the same *kind* of table and none of
them print it the same way. This module holds what they genuinely share; what
differs stays in the adapters.

Surveyed before writing any of it — six layouts across four institutions:

| Layout       | Dates | Amount columns              | Direction from   |
|--------------|-------|-----------------------------|------------------|
| Trust acc    | 1–2   | FCY + SGD                   | leading `+`      |
| Trust cc     | 1–2   | FCY + SGD                   | leading `+`      |
| DBS acc      | 1     | Withdrawal, Deposit, Balance| which column     |
| DBS cc       | 1     | Amount                      | `CR` suffix      |
| MariBank acc | 1     | Outgoing, Incoming          | which column     |
| MariBank cc  | 2     | Amount                      | explicit `-`     |
| OCBC cc      | 1     | Amount                      | `CR` suffix      |

What they share, and what lives here:

1. A header row fixes the columns, and every later line is cut into cells by
   x-position.
2. A row is a line carrying a value in a money column. Anything else is not a
   transaction — which is what keeps section headings and page furniture out
   of the ledger.
3. Descriptions wrap onto neighbouring lines, above the row or below it.

What deliberately does **not** live here:

- **How direction is read.** Three incompatible conventions in the table above
  — a leading `+`, a `CR` suffix, and the column itself — and a fourth will
  turn up. Only the positional case is expressible here, as `Column.sign`.
- **Which labels mean opening and closing.** Institution-specific wording:
  "Previous balance", "Balance Brought Forward", "LAST MONTH'S BALANCE".
- **How a period or an account reference is found.** Nothing in common.

The engine was written only after all six layouts were measured, and each rule
in it exists because at least two of them need it. `Trust` still has its own
row assembler: porting it is a refactor of the one institution already carrying
78 documents, with no functional gain, so it waits until there is a reason
beyond tidiness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .pdfio import Line

#: Column roles. Only MONEY and BALANCE columns make a line a row; TEXT never
#: does, which is what stops a section heading from becoming a transaction.
DATE = "date"
TEXT = "text"
MONEY = "money"
BALANCE = "balance"

#: A word's left edge is where a left-aligned column starts; a numeric column
#: is aligned on its right edge instead, and matching on the wrong one puts
#: amounts in the wrong column.
LEFT = "left"
RIGHT = "right"

#: How far left of the first column a word may still belong to the table.
MARGIN_SLACK = 2.0


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    role: str
    #: The header word's span. Text columns are found by their left edge,
    #: money columns by their right, because that is how each is aligned.
    left: float
    right: float
    #: Only meaningful for MONEY columns whose *position* carries the
    #: direction — DBS's Withdrawal/Deposit, MariBank's Outgoing/Incoming.
    #: Zero means the direction is written in the text instead.
    sign: int = 0


@dataclass(frozen=True, slots=True)
class TableSpec:
    columns: tuple[Column, ...]
    #: How a description-only line is attached.
    #:
    #: A distance means "belongs to the row above if within this many points of
    #: the last line already attached to it, otherwise it is a lead-in for the
    #: row below" — Trust prints merchant names above their row as well as
    #: below, so it needs the distinction and its wraps sit ~6pt away against a
    #: 25pt pitch.
    #:
    #: Measured line to line rather than from the row, so this is the pitch of
    #: one wrapped line and not the total depth of the wrap. A description that
    #: runs onto three lines is as attachable as one that runs onto one.
    #:
    #: `None` means "always belongs to the row above". Layouts that only ever
    #: wrap downwards want this: DBS trails up to four reference lines under a
    #: transaction, the last of them 42pt down against a 48pt pitch, and any
    #: fixed threshold either clips the last line or risks swallowing the next
    #: row's.
    continuation_gap: float | None = 9.0
    #: What a money cell must look like for the line to count as a row.
    #:
    #: Without it, any stray mark landing in an amount column makes a row.
    #: DBS renders part of its registration text as loose characters strewn
    #: across the page, and a line reading "geR 4 4 4 4 4" put a "4" in both
    #: the withdrawal and the deposit column — enough to fail nine statements
    #: on a guard meant to catch genuine ambiguity. An amount in these layouts
    #: always carries two decimal places; a bare "4" is not money.
    money_pattern: "re.Pattern | None" = None

    def is_money(self, text: str) -> bool:
        if not text:
            return False
        return self.money_pattern is None or bool(self.money_pattern.search(text))

    def named(self, role: str) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.role == role)

    @property
    def money_columns(self) -> tuple[Column, ...]:
        return self.named(MONEY)

    @property
    def value_columns(self) -> tuple[Column, ...]:
        """Columns whose presence makes a line a row."""
        return self.named(MONEY) + self.named(BALANCE)

    @property
    def text_columns(self) -> tuple[Column, ...]:
        return self.named(DATE) + self.named(TEXT)

    @property
    def money_zone(self) -> float:
        """Where the money columns begin.

        The leftmost money heading's left edge. Descriptions run up to it and
        amounts start after it, on every layout measured.
        """
        return min(c.left for c in self.value_columns)

    def cells(self, line: Line) -> dict[str, str]:
        """Cut a line into named cells.

        Two rules, because the two halves of these tables are aligned
        differently and using one rule for both misfiles data:

        - **Left of the money zone**, a word joins the text column whose left
          edge it sits at or after. Descriptions are left-aligned and can run
          long — Trust's reach 175pt past their heading — so nearest-anchor
          would pull their tails into the amount column.
        - **At or right of it**, a word joins the money column whose *right*
          edge is nearest. Amounts are right-aligned independently of their
          headings: a DBS balance starts 8pt left of the word "Balance" and a
          withdrawal 30pt right of "Withdrawal", so only the right edge is
          reliable.
        """
        buckets: dict[str, list[str]] = {c.name: [] for c in self.columns}
        text_columns = sorted(self.text_columns, key=lambda c: c.left)
        money_columns = self.value_columns
        boundary = self.money_zone
        # Anything ending before the first column starts is not table content.
        # DBS prints its company registration numbers rotated down the left
        # margin, and where one lands on a transaction's baseline it would
        # otherwise be read as part of that row's date.
        margin = (text_columns[0].left - MARGIN_SLACK) if text_columns else float("-inf")

        for word in line.words:
            if word.x1 <= margin:
                continue
            if word.x0 < boundary or not money_columns:
                match = None
                for column in text_columns:
                    if word.x0 >= column.left - 1.0:
                        match = column
                if match is None:
                    match = text_columns[0] if text_columns else self.columns[0]
            else:
                match = min(money_columns, key=lambda c: abs(word.x1 - c.right))
            buckets[match.name].append(word.text)

        return {name: " ".join(parts).strip() for name, parts in buckets.items()}


@dataclass(frozen=True, slots=True)
class Row:
    """One assembled table row, before an adapter interprets it."""

    line: Line
    cells: dict[str, str]
    #: Description fragments from neighbouring lines, in reading order: the
    #: first `lead_ins` of them were printed *above* the row, the rest below.
    #: Kept in order so a wrapped merchant name reads the way it was printed.
    fragments: tuple[str, ...] = ()
    lead_ins: int = 0

    def cell(self, name: str) -> str:
        return self.cells.get(name, "")

    def description(self, *names: str) -> str:
        """The row's own text plus everything that wrapped around it."""
        own = " ".join(self.cell(n) for n in names if self.cell(n))
        parts = [*self.fragments[: self.lead_ins], own, *self.fragments[self.lead_ins:]]
        return " ".join(p for p in parts if p).strip()

    @property
    def label(self) -> str:
        """Lower-cased row text, for matching balance markers."""
        return self.line.text.strip().lower()


def columns_from_header(
    header: Line,
    spec: list[tuple[str, str, str, int]],
) -> tuple[Column, ...]:
    """Locate each column by a word in the header row.

    `spec` entries are (column name, header word prefix or prefixes, role,
    sign). Each is matched against the header's words in order, so a heading
    that appears twice — Trust prints "Amount" for both its currency columns —
    resolves left to right.

    A tuple of prefixes accepts synonyms. Institutions reword their headings
    without changing the table: "Withdrawal" and "Debit" and "Money Out" are
    the same column, and naming the alternatives here keeps a rewording out of
    the code.

    The whole header word span is kept. Which edge matters depends on the
    column's role, and that is decided in `TableSpec.cells`.
    """
    columns: list[Column] = []
    used: set[int] = set()
    for name, prefixes, role, sign in spec:
        options = (prefixes,) if isinstance(prefixes, str) else tuple(prefixes)
        index = next(
            (i for i, w in enumerate(header.words)
             if i not in used and any(w.text.lower().startswith(o.lower()) for o in options)),
            None,
        )
        if index is None:
            raise LookupError(
                f"header has no column starting {' or '.join(repr(o) for o in options)}: "
                f"{header.text!r}"
            )
        used.add(index)
        word = header.words[index]
        columns.append(Column(name, role, word.x0, word.x1, sign))
    return tuple(columns)


def assemble_rows(
    lines: list[Line],
    spec: TableSpec,
    *,
    skip: "re.Pattern | None" = None,
) -> list[Row]:
    """Turn visual lines into table rows, rejoining descriptions that wrapped.

    A line becomes a row when it carries a value in a money or balance column.
    Everything else is either page furniture, a section heading, or a piece of
    a description that wrapped — and a wrapped piece belongs to the row it sits
    nearest, above it or below it.

    Getting the "below" case wrong is not cosmetic: the fragment is otherwise
    carried forward onto the *next* row, corrupting two descriptions, and
    `description_norm` feeds the dedupe key.

    Three rules keep a fragment with the row that printed it, and each exists
    because a real statement broke without it:

    - **The gap is measured from the last line attached to the row, not from
      the row itself.** A description that wraps onto three lines steps down one
      line at a time, so measuring from the row makes the distance accumulate
      and the wrap stops attaching part-way through. OCBC trails a card number,
      then the channel, then the country under a bill payment, each ~11pt below
      the last: the first attached, the second was held over and prepended to
      the *next* row, and the third was dropped.
    - **A fragment stays on its own page.** `pending` used to survive a page
      break, and `line.top` resets at the top of a page, so the previous page's
      footer read as sitting "above" the first row of the next one — OCBC card
      statements imported "HCBS250101(000000) 0000 EXAMPLE MERCHANT SINGAPORE"
      as a merchant.
    - **A lead-in must actually be above its row.** The distance test had no
      lower bound, so anything left pending qualified however far below it sat.
    """
    rows: list[Row] = []
    #: Carried with its position: a fragment only leads into a row it is near,
    #: on the same page.
    pending: list[tuple[int, float, str]] = []
    #: Page and baseline of the last line belonging to the row being built —
    #: the row itself, or the last fragment attached under it.
    anchor: tuple[int, float] | None = None
    text_names = [c.name for c in spec.text_columns]
    value_names = [c.name for c in spec.value_columns]

    def wraps_under(line: Line, page: int, top: float) -> bool:
        """Is this line close enough under (page, top) to be its continuation?"""
        return (
            line.page_number == page and line.top > top
            and (spec.continuation_gap is None or line.top - top <= spec.continuation_gap)
        )

    for line in lines:
        if not line.text.strip() or (skip is not None and skip.match(line.text)):
            continue

        cells = spec.cells(line)
        has_value = any(spec.is_money(cells.get(name, "")) for name in value_names)

        if not has_value:
            fragment = " ".join(cells.get(name, "") for name in text_names).strip()
            if not fragment:
                continue
            if rows and anchor is not None and wraps_under(line, *anchor):
                previous = rows[-1]
                rows[-1] = Row(
                    previous.line, previous.cells,
                    previous.fragments + (fragment,), previous.lead_ins,
                )
                # The wrap continues from here, not from the row.
                anchor = (line.page_number, line.top)
            else:
                pending.append((line.page_number, line.top, fragment))
            continue

        # A fragment above the row joins it only if it is as close as one below
        # would have to be. Without the distance test a category heading
        # printed over the first row of its section — MariBank's "Purchase" —
        # became the start of that row's description.
        lead_ins = tuple(
            text for page, top, text in pending
            if page == line.page_number and 0 < line.top - top
            and (spec.continuation_gap is None or line.top - top <= spec.continuation_gap)
        )
        rows.append(Row(line, cells, lead_ins, len(lead_ins)))
        pending = []
        anchor = (line.page_number, line.top)

    return rows


def money_cells(row: Row, spec: TableSpec) -> list[tuple[Column, str]]:
    """The money columns this row holds an actual amount in."""
    return [
        (c, row.cell(c.name))
        for c in spec.money_columns
        if spec.is_money(row.cell(c.name))
    ]
