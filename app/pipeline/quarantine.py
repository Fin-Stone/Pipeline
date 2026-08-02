"""Quarantine: fail one document loudly, keep the run going.

One unparseable statement must never stop the others from importing. The
original is already in the content-addressed store, so nothing is lost and
`reparse` can replay it once the adapter is fixed.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

REASON_SUFFIX = ".reason.json"


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


def clear_reason(quarantine_dir: Path, sha256: str) -> bool:
    """Remove a document's reason file, before it is reparsed.

    A stale reason describing a failure that has since been fixed is worse
    than none: it makes `finstone report` lie about the current state.
    """
    target = quarantine_dir / f"{sha256}{REASON_SUFFIX}"
    if target.exists():
        target.unlink()
        return True
    return False


def depth(quarantine_dir: Path) -> int:
    if not quarantine_dir.exists():
        return 0
    return sum(1 for _ in quarantine_dir.glob(f"*{REASON_SUFFIX}"))
