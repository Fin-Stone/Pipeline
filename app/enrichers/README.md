# Enrichers

**Empty, and not where the enrichment currently lives.** Category rules,
recurrence detection and transfer matching all landed in `app/domain/` beside
the other rules, with their orchestration in `app/pipeline/` — because they are
pure functions over the ledger and belong with the rest of the pure functions,
not in a folder that was named before they were written.

This is kept for the two tiers of architecture §3.1 that genuinely are a
different kind of thing:

- **k-NN against the household's own labelled history**, embedding
  `description_norm` into `pgvector`, with distance as confidence.
- **The model tier for the residual**, batched, recording `model_version` and a
  prompt hash on the row so a result can be reproduced.

Both reach outside the process — one to a vector index, one to a model endpoint
— so both need a port in `app/ports/` before they need a module here. See
[../../docs/roadmap.md](../../docs/roadmap.md), Alpha 3.
