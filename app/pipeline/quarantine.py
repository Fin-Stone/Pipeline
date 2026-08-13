"""Quarantine: fail one document loudly, keep the run going.

One unparseable statement must never stop the others from importing. The
original is already in the content-addressed store, so nothing is lost and
`reparse` can replay it once the adapter is fixed.
"""

from __future__ import annotations

import json
import re
import shutil
import traceback
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

REASON_SUFFIX = ".reason.json"

#: Characters that cannot appear inside a single filename on every filesystem
#: this runs on. Path separators are in the set, which is what collapses a
#: relative path into one recognisable name rather than a tree of directories.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _plain(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    return value


def write_reason(
    quarantine_dir: Path,
    sha256: str,
    *,
    failure_class: str,
    message: str,
    source_profile: str | None = None,
    source_relpath: str | None = None,
    detail: dict | None = None,
    error: BaseException | None = None,
) -> Path:
    """Record why a document was rejected, next to nothing else.

    The reason file is the operator's entire diagnostic surface for a failed
    import, so it carries the fingerprint, the expected-versus-actual numbers
    and the traceback rather than a bare message.
    """
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "sha256": sha256,
        "quarantined_at": datetime.now(timezone.utc).isoformat(),
        "failure_class": failure_class,
        "message": message,
        # Recorded so a reason file says which tenant's it is without relying on
        # the directory it happens to be sitting in.
        "source_profile": source_profile,
        "source_relpath": source_relpath,
        "detail": _plain(detail or {}),
    }
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["traceback"] = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )

    target = quarantine_dir / f"{sha256}{REASON_SUFFIX}"
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def export_name(sha256: str, source_relpath: str | None) -> str:
    """A name saying which document this is, and which reason file it belongs to.

    The digest prefix ties the file to its `<sha256>.reason.json` neighbour; the
    flattened source path is the half a person recognises. The original
    extension rides along on the end of the path, so the file still opens.
    """
    stem = _ILLEGAL.sub("-", (source_relpath or sha256).strip()).strip("-")
    return f"{sha256[:8]}-{stem or sha256}"


def export_originals(quarantine_dir: Path, files_dir: Path, blob_store) -> list[Path]:
    """Copy every quarantined original out under a name a person can recognise.

    Read from the blob store rather than from uploads/, because the store is the
    immutable record of what actually failed. The upload may since have been
    re-downloaded, renamed or moved, and then it is no longer evidence.

    Syncs rather than accumulates, for the reason `clear_reason` gives: an export
    whose reason file has gone describes a failure that no longer exists.
    """
    wanted: dict[Path, str] = {}
    if quarantine_dir.exists():
        for reason_file in sorted(quarantine_dir.glob(f"*{REASON_SUFFIX}")):
            try:
                payload = json.loads(reason_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            sha256 = payload.get("sha256")
            if sha256:
                wanted[files_dir / export_name(sha256, payload.get("source_relpath"))] = sha256

    files_dir.mkdir(parents=True, exist_ok=True)
    for present in files_dir.iterdir():
        # Never a reason file, whatever the two directories are configured to
        # be: sweeping away the record of a failure is not this function's job.
        if present.is_file() and present not in wanted and not present.name.endswith(REASON_SUFFIX):
            present.unlink()

    written = []
    for target, sha256 in sorted(wanted.items()):
        # The store is content-addressed, so a file already at this name holds
        # exactly these bytes. Re-exporting is a no-op rather than a rewrite.
        if not target.exists():
            if not blob_store.exists(sha256):
                continue
            with blob_store.open(sha256) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
        written.append(target)
    return written


def read_reason(quarantine_dir: Path, sha256: str) -> dict | None:
    """Why one document was rejected, or `None` if nothing here says.

    The reason is a file rather than a column because it holds a traceback and
    the expected-versus-actual detail, and none of that belongs in the ledger.
    That makes it invisible to anything reading the database alone — which is
    why this exists: an API serving a dashboard has to be able to answer "why"
    without shelling into the container to read JSON off a disk.
    """
    target = quarantine_dir / f"{sha256}{REASON_SUFFIX}"
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_reason(quarantine_dir: Path, sha256: str, files_dir: Path | None = None) -> bool:
    """Remove a document's reason file and any export of it, before reparsing.

    A stale reason describing a failure that has since been fixed is worse
    than none: it makes `finstone report` lie about the current state. An
    exported original is the same lie in a form someone can double-click.
    """
    if files_dir is not None and files_dir.exists():
        for export in files_dir.glob(f"{sha256[:8]}-*"):
            export.unlink()

    target = quarantine_dir / f"{sha256}{REASON_SUFFIX}"
    if target.exists():
        target.unlink()
        return True
    return False


def depth(quarantine_dir: Path) -> int:
    if not quarantine_dir.exists():
        return 0
    return sum(1 for _ in quarantine_dir.glob(f"*{REASON_SUFFIX}"))
