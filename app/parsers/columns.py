"""Column bands for positioned statement tables.

Amounts in a statement are right-aligned inside their column, so "which column
is this number in" is a question about x-position, not word order. Bands are
derived from the table's own header row rather than hardcoded, so a layout that
shifts its columns by a few points does not break the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass

from .pdfio import Line


@dataclass(frozen=True, slots=True)
class ColumnBands:
    """Named half-open x ranges covering a table's width."""

    names: tuple[str, ...]
    starts: tuple[float, ...]

    @classmethod
    def from_starts(cls, pairs: list[tuple[str, float]]) -> "ColumnBands":
        ordered = sorted(pairs, key=lambda p: p[1])
        return cls(tuple(n for n, _ in ordered), tuple(x for _, x in ordered))

    def bounds(self, name: str) -> tuple[float, float]:
        index = self.names.index(name)
        low = self.starts[index]
        high = self.starts[index + 1] if index + 1 < len(self.starts) else float("inf")
        return low, high

    def cell(self, line: Line, name: str) -> str:
        low, high = self.bounds(name)
        return line.text_between(low, high).strip()

    def cells(self, line: Line) -> dict[str, str]:
        return {name: self.cell(line, name) for name in self.names}
