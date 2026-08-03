"""Vendor strings the pipeline has proven belong to an adapter.

Institutions rename the tool that renders their statements. DBS's went
"Quadient Group AG~Inspire" to "Quadient CXM AG~Inspire" to "Quadient~Inspire"
— one product, a company that renamed itself twice, 107 statements that stopped
routing. Trust's went "Skia/PDF m80" to "Skia/PDF m141".

Rather than requiring a code change each time, an unrecognised vendor string is
treated as a **rename to be proven or rejected**: the document is parsed with
the adapter whose header lines it matches, and accepted only if the result
reconciles to the cent against the statement's own balances. What worked is
recorded here so the next statement routes directly instead of being
rediscovered.

This is not the "never guess" rule being relaxed. Guessing is choosing without
evidence; this proposes and then *verifies* against an oracle the document
carries with it. What is relaxed is only the vendor string — the header lines
that identify the format still have to match, and always did.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class LearnedVendor:
    """A producer/creator pair proven to belong to an adapter."""

    adapter: str
    producer: str
    creator: str
    learned_at: str = ""
    #: The document that proved it, and what reconciled. Kept so a learned rule
    #: can be audited later rather than taken on trust.
    evidence: dict = field(default_factory=dict)

    def matches(self, producer: str, creator: str) -> bool:
        return self.producer == producer and self.creator == creator


@dataclass
class LearnedRules:
    path: Path | None = None
    vendors: list[LearnedVendor] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None) -> "LearnedRules":
        if path is None or not path.exists():
            return cls(path=path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt store must not stop ingestion; it only costs the
            # shortcut, and healing will rediscover what it held.
            return cls(path=path)
        return cls(
            path=path,
            vendors=[
                LearnedVendor(
                    adapter=v["adapter"], producer=v.get("producer", ""),
                    creator=v.get("creator", ""), learned_at=v.get("learned_at", ""),
                    evidence=v.get("evidence", {}),
                )
                for v in payload.get("vendors", [])
            ],
        )

    def adapters_for(self, producer: str, creator: str) -> list[str]:
        return [v.adapter for v in self.vendors if v.matches(producer, creator)]

    def knows(self, adapter: str, producer: str, creator: str) -> bool:
        return any(
            v.adapter == adapter and v.matches(producer, creator) for v in self.vendors
        )

    def record(self, adapter: str, producer: str, creator: str, evidence: dict) -> LearnedVendor:
        vendor = LearnedVendor(
            adapter=adapter, producer=producer, creator=creator,
            learned_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            evidence=evidence,
        )
        self.vendors.append(vendor)
        self.save()
        return vendor

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": FORMAT_VERSION,
            "note": (
                "Vendor strings proven to belong to an adapter by reconciling a "
                "statement to the cent. Safe to delete: anything here will be "
                "relearned the next time such a statement is ingested."
            ),
            "vendors": [
                {
                    "adapter": v.adapter, "producer": v.producer, "creator": v.creator,
                    "learned_at": v.learned_at, "evidence": v.evidence,
                }
                for v in self.vendors
            ],
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def __len__(self) -> int:
        return len(self.vendors)
