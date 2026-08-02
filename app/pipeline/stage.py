"""uploads/<profile>/ -> data/inbox/

The one place the prod/dummy distinction is enforced. Keeping it here rather
than spreading it through the pipeline means every downstream component sees a
single, uniform inbox and cannot accidentally reach into real data.

Nothing is ever deleted from uploads/. It belongs to the operator.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import DOCUMENT_EXTENSIONS, Config
from ..domain.dedupe import sha256_file
from ..ports.source import DiscoveredFile


class LocalDirectorySource:
    """Recursive walk of a directory, yielding candidate documents."""

    def __init__(self, root: Path, extensions: tuple[str, ...] = DOCUMENT_EXTENSIONS):
        self._root = Path(root)
        self._extensions = tuple(e.lower() for e in extensions)

    def discover(self):
        if not self._root.exists():
            return
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in self._extensions:
                continue
            yield DiscoveredFile(path=path, relpath=path.relative_to(self._root).as_posix())


@dataclass(frozen=True, slots=True)
class StageResult:
    profile: str
    discovered: int = 0
    staged: int = 0
    already_known: int = 0
    already_staged: int = 0
    paths: tuple[str, ...] = ()


def stage(config: Config, profile: str, repository=None) -> StageResult:
    """Copy new documents from uploads/<profile>/ into the inbox.

    Idempotent twice over: a file whose digest is already in the ledger is
    skipped, and a file already sitting in the inbox is not re-copied. That
    means reorganising folders under uploads/ never causes a re-import.
    """
    config.require_profile_allowed(profile)
    source_root = config.uploads_for(profile)
    source = LocalDirectorySource(source_root)

    discovered = staged = already_known = already_staged = 0
    paths: list[str] = []

    for found in source.discover():
        discovered += 1
        digest = sha256_file(found.path)

        if repository is not None and repository.get_document_id(digest) is not None:
            already_known += 1
            continue

        target = config.inbox_dir / profile / found.relpath
        if target.exists() and sha256_file(target) == digest:
            already_staged += 1
            paths.append(str(target))
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(found.path, target)
        staged += 1
        paths.append(str(target))

    return StageResult(
        profile=profile,
        discovered=discovered,
        staged=staged,
        already_known=already_known,
        already_staged=already_staged,
        paths=tuple(paths),
    )
