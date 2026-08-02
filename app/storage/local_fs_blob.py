"""Content-addressed original storage on local disk.

Originals are written once under their digest and never mutated. Sharding by
the first four hex characters keeps directory sizes sane on every filesystem.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class LocalFsBlobStore:
    def __init__(self, root: Path):
        self._root = Path(root)

    def _relative(self, sha256: str) -> Path:
        if len(sha256) != 64:
            raise ValueError(f"expected a sha256 hex digest, got {sha256!r}")
        return Path(sha256[:2]) / sha256[2:4] / sha256

    def path_for(self, sha256: str) -> str:
        return str(self._root / self._relative(sha256))

    def exists(self, sha256: str) -> bool:
        return (self._root / self._relative(sha256)).exists()

    def put(self, source: Path, sha256: str) -> str:
        """Store the file under its digest. Idempotent.

        Content already present is left exactly as it is — the store is
        immutable, and identical content by definition needs no update.
        """
        target = self._root / self._relative(sha256)
        if target.exists():
            return str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary name first so an interrupted copy can never
        # leave a truncated file sitting at a path that claims to be complete.
        staging = target.with_name(target.name + ".partial")
        shutil.copy2(source, staging)
        staging.replace(target)
        return str(target)

    def open(self, sha256: str):
        return (self._root / self._relative(sha256)).open("rb")
