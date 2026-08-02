"""The immutable original-document seam.

Originals are content-addressed and never mutated. Keeping the bytes forever is
what makes `reparse` possible: an adapter bugfix can be replayed over years of
history without re-downloading anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class BlobStore(Protocol):
    def put(self, source: Path, sha256: str) -> str:
        """Store the bytes under their digest and return the storage path.

        Must be idempotent: storing content that is already present is a no-op
        that returns the same path.
        """

    def path_for(self, sha256: str) -> str:
        """The storage path a digest maps to, whether or not it exists yet."""

    def exists(self, sha256: str) -> bool: ...

    def open(self, sha256: str):
        """Open the stored original for reading, in binary mode."""
