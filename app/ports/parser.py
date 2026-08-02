"""The statement-adapter seam.

One adapter per (institution, doc_type, layout). Adapters are selected by
fingerprint and never by guessing — see app/parsers/registry.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.models import ParsedDocument


class ParseError(Exception):
    """Raised by an adapter when a document does not match its expectations.

    Always a quarantine, never a partial import. An adapter that is unsure
    must raise rather than return incomplete data.

    `context` carries whatever the adapter knew about *where* the failure was:
    the page, the vertical position, the raw line and how it was split into
    columns. That is what lets a failure be diagnosed from a pasted report
    instead of from the statement itself.
    """

    def __init__(self, message: str, *, context: dict | None = None):
        super().__init__(message)
        self.context = context or {}

    def with_context(self, **fields) -> "ParseError":
        self.context.update(fields)
        return self


@dataclass(frozen=True, slots=True)
class AdapterMatch:
    adapter: "StatementAdapter"
    fingerprint: str


@runtime_checkable
class StatementAdapter(Protocol):
    #: Stable identity, recorded on every row the adapter produces so a bad
    #: parse can be traced to the exact code that produced it.
    name: str
    version: str
    institution: str
    doc_type: str

    def parse(self, path: Path, *, password: str | None = None) -> ParsedDocument:
        """Parse a document, or raise ParseError."""
