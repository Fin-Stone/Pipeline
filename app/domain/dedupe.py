"""Deduplication keys.

`txn.dedupe_key` is UNIQUE, and together with the UNIQUE `source_document.sha256`
it is what makes every retry in the pipeline safe. Getting the `seq` rule right
is what makes it *correct* as well as safe.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence

from .models import ParsedTxn
from .normalise import normalise_description


def dedupe_key(account_natural_key: str, posted_date, amount_minor: int, description_norm: str, seq: int) -> str:
    payload = "\x1f".join([
        account_natural_key,
        posted_date.isoformat(),
        str(amount_minor),
        description_norm,
        str(seq),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assign_seq(txns: Sequence[ParsedTxn]) -> list[int]:
    """Assign each transaction its index within its identical-row group.

    `seq` counts rows sharing (posted_date, amount_minor, description_norm),
    numbered in the order the source document lists them. That ordering is the
    whole point:

    - re-importing the same document yields identical keys, so nothing
      duplicates;
    - two overlapping statements listing the same rows in the same order yield
      identical keys, so the overlap collapses correctly;
    - two genuine $4.50 coffees on the same day get seq 0 and 1, so both
      survive.

    Known limitation, documented in docs/ingestion.md: if an institution
    reorders same-key rows between two overlapping statements, seq assignment
    can differ and a row may import twice. The alternative — deriving seq from
    what is already in the database — breaks idempotency outright, which is a
    worse trade. Monthly balance reconciliation catches the resulting drift.
    """
    counters: dict[tuple, int] = defaultdict(int)
    out = []
    for txn in txns:
        group = (txn.posted_date, txn.amount_minor, normalise_description(txn.description_raw))
        out.append(counters[group])
        counters[group] += 1
    return out


def keys_for(account_natural_key: str, txns: Sequence[ParsedTxn]) -> list[str]:
    """Compute the dedupe key for every transaction in an account."""
    seqs = assign_seq(txns)
    return [
        dedupe_key(
            account_natural_key,
            txn.posted_date,
            txn.amount_minor,
            normalise_description(txn.description_raw),
            seq,
        )
        for txn, seq in zip(txns, seqs, strict=True)
    ]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def iter_unique(keys: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out
