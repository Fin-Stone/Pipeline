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
    worse trade.

    What catches the resulting drift is `app.domain.reconcile`, reachable as
    `finstone reconcile` and `GET /api/v1/reconciliation`: a row the ledger
    holds twice is movement the banks never declared, between two closing
    balances that both know better.
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


def statement_key(doc_type: str, period_start, period_end, account_refs: Iterable[str]) -> str:
    """Identify a *statement*, independent of the file that carried it.

    A bank issues one statement per account per period. That is the identity,
    and it is what should decide whether two uploads are the same statement —
    not the bytes, which change whenever a PDF is re-downloaded, re-saved,
    renamed or passed through anything that touches its metadata.

    This matters most for shared accounts. In a household where two members
    both diligently upload the joint account's statement, byte-level identity
    catches only the case where they happen to have the identical file;
    everything else used to import as a second document whose every row
    deduplicated away, leaving balances attached to nothing.

    Keyed on the account references rather than the file, so a re-download is
    recognised as the same statement no matter what happened to it in between.
    """
    refs = "|".join(sorted(set(account_refs)))
    payload = "\x1f".join([
        doc_type,
        period_start.isoformat() if period_start else "",
        period_end.isoformat() if period_end else "",
        refs,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
