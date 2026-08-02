"""The document-source seam.

Phase 1 walks a local directory. Later phases add IMAP attachment fetch and the
automated retrieval tier; both implement this same protocol and write into the
same inbox, so nothing downstream changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    path: Path
    #: Path relative to the source root, preserved so a ledger row can always
    #: be traced back to a file the operator recognises.
    relpath: str


@runtime_checkable
class DocumentSource(Protocol):
    def discover(self) -> Iterator[DiscoveredFile]:
        """Yield every candidate document, recursively."""
