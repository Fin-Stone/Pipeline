# Handoff: personal data in git history, and how to keep it out

Audit date: 2026-08-13. Scope: all commits reachable from `main`.

This document deliberately contains **no real values** — describing the leak by
shape and location rather than by content is the whole point. The raw values are
in the mirror backup named at the bottom, and nowhere else.

## Status

| | |
|---|---|
| Live in the working tree | **Cleared** — commits `fd48447`, `6339638` (now rewritten) |
| Reachable in history | **Cleared** — `git-filter-repo`, all 96 commits rewritten |
| Pushed to `origin` | **Not yet.** `origin/main` still holds the unscrubbed history |
| Tests | 732 passed, 6 skipped — unchanged before and after the rewrite |
| `HEAD` tree hash | Identical before and after the rewrite (`c1a9680`) |
| Recurrence guard | Added — `tests/test_no_personal_data.py`, suite now 740 passed |

**The remaining action is a force-push.** Until it happens, everything below is
still on GitHub.

## What was found, and where

### A. Live in `HEAD` — and already pushed

All of it entered in two commits, `66b1426` and `8681ca2` (2026-08-12), pasted
out of real statements into layout fixtures.

| What | Shape | Where |
|---|---|---|
| Mastercard PAN | 16 digits, Luhn-valid, real BIN | `tests/test_ocbc_layouts.py` ×3, `tests/test_tables.py` ×2 |
| OCBC 360 account number | 12 digits | `tests/test_ocbc_layouts.py` |
| HSBC document reference + card last four | `HCBS<6>(<6>) <4>` | `app/parsers/tables.py`, `tests/test_adapters_dummy.py`, `tests/test_tables.py` |
| Merchant street address, postcode, UEN | `#NN-NN`, 6-digit postcode, `TNNXXNNNNX` | `app/parsers/trust/base.py`, `tests/test_trust_layouts.py`, `tests/test_adapters_dummy.py` |
| Card last four in sample filenames | `-NNNN-Mon-YY.pdf` | `tests/test_adapters_dummy.py` ×3 |
| Probable home postcode + a surname | 6 digits; a name token | `tests/test_adapters_dummy.py` |

The PAN was confirmed genuine rather than invented two ways: it passes Luhn, and
its last four match the card number in an OCBC sample filename that commit
`5246b98` had already scrubbed for exactly that reason.

### B. History-only — scrubbed from the tip earlier, still recoverable

Commits `97bbc66`, `9bb903b`, `0d5e83b`, `5246b98` (Aug 3–10) removed these from
the working tree. That is not the same as removing them from the repository:
every one was still recoverable with `git log -S`.

- Insurance policy and GIRO references (five distinct formats)
- The household's insurers, one with its exact annual premium
- Merchants naming the neighbourhood the household shops and eats in
- Account balances, and one payment annotated in-repo as a month's salary
- Nineteen-digit statement document ids embedded in sample filenames
- A transfer recipient's first name

### C. Confirmed clean — no action needed

- No PDF, CSV, database or `.env` file has ever been committed.
- No API keys, tokens or credentials.
- `category/`, `proposal*.json`, `residual*.json`, `dist/` hold a lot of real
  ledger data but are correctly gitignored and have **0 commits** each. The
  `.gitignore` reasoning is sound; leave it alone.
- `@example.com` addresses and `4111…` card numbers in tests are correct
  synthetic placeholders.
- The 16-digit keys in `app/parsers/glyph_tables/opentext.json` are OCR glyph
  hashes, not card numbers. **Any scanner you add must not flag these.**
- `IKEA-RESTAURANT` is retained deliberately. It is a named seed rule
  (`app/domain/seed_rules.py`) encoding that a retailer's restaurant is Dining.
  A global brand, negligible as personal data, and load-bearing product logic.

## Why the earlier scrub did not hold

Worth understanding, because the fix has to address the cause and not the
symptom:

1. **It only changed the tip.** A normal commit that replaces a value leaves the
   original one `git log -S` away. A history rewrite was run once (Aug 5) but
   covered only what `97bbc66` addressed.
2. **Nothing stopped it coming back.** Four days after the scrub, `66b1426` and
   `8681ca2` reintroduced fresh statement data with no friction at all. There was
   no check, local or in CI, that would have objected.

Point 2 is the one that matters. Manual discipline already failed here once,
with an author who was clearly *trying* — the scrub commits are thoughtful and
the `.gitignore` is genuinely well-reasoned. The gap is enforcement.

## Recommended: detect it automatically

### Layer 1 — a test, blocking merges through the check that already exists

**Implemented: [tests/test_no_personal_data.py](tests/test_no_personal_data.py).**

`.github/workflows/test.yml` already runs `pytest` on every PR to `main`. A
detector written as a test therefore needs no new workflow and no new service:
it fails the check that is already there. It scans every tracked text file for
Luhn-valid card numbers, NRIC/FIN, UEN, unit numbers and SG phone numbers, and
names each finding as `path:line: rule: value`.

Four notes on the design, three of them learned by running it:

- **Luhn is what makes the card rule usable.** A bare "16 consecutive digits"
  rule fires on lockfile hashes, on the glyph tables and on every timestamp; that
  false-positive rate is how a check ends up disabled. Requiring Luhn cuts it by
  about 90%.
- **It is a filter, not a decision.** Roughly one arbitrary digit-run in ten
  passes Luhn anyway. One of the nineteen-digit statement document ids in this
  repo is Luhn-valid when truncated to sixteen, which is exactly the case that
  needs an `ALLOWED` entry rather than a cleverer regex.
- **`ALLOWED` has to exist, and it is load-bearing.** `4111…` and `5555…` are
  Luhn-valid by design, and the synthetic UEN and unit number in the Trust wrap
  fixtures match the UEN and unit-number rules. Every entry carries its reason;
  that list is the honest record of what is deliberately synthetic.
- **No postcode rule**, on purpose. A bare `\d{6}` collides with amounts in
  minor units, which this codebase is full of. Postcodes are guarded where they
  matter instead: `tests/test_adapters_dummy.py` asserts that parser signatures
  contain no postcode, unit number or street line.

The file also tests the detector against itself — planting the original card
number and asserting it is caught, and asserting the allowlisted and skipped
cases are not. A guard that cannot fail is worse than none, because it reads as
coverage.

### Layer 2 — a pre-commit hook, so it fails in one second rather than in CI

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: no-personal-data
        name: no personal data in tracked files
        entry: python -m pytest -q tests/test_no_personal_data.py
        language: system
        pass_filenames: false
        always_run: true
```

`pip install pre-commit && pre-commit install`. Same check, immediate feedback,
and it cannot be forgotten once installed. It *can* be bypassed with
`--no-verify`, which is why Layer 1 exists in CI as well.

### Layer 3 — history scanning, for what a tree scan cannot see

Layers 1 and 2 look at the current tree. They would not have caught Section B,
where the tip was clean and the history was not. Add a job that scans the commits
a PR actually introduces:

```yaml
# in .github/workflows/test.yml, as a third job
  no-personal-data-in-history:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # gitleaks needs real history
      - uses: gitleaks/gitleaks-action@v2
        env:
          GITLEAKS_CONFIG: .gitleaks.toml
```

with a `.gitleaks.toml` carrying the same shapes:

```toml
title = "Finstone personal data"
[extend]
useDefault = true               # keeps the API-key rules

[[rules]]
id = "sg-nric"
description = "Singapore NRIC/FIN"
regex = '''\b[STFGM]\d{7}[A-Z]\b'''

[[rules]]
id = "sg-uen"
description = "Singapore UEN"
regex = '''\b[T-Z]\d{2}[A-Z]{2}\d{4}[A-Z]\b'''

[[rules]]
id = "unit-number"
description = "Singapore unit number"
regex = '''#\d{2}-\d{2,4}\b'''

[allowlist]
paths = ['''app/parsers/glyph_tables/.*''']
regexTarget = "match"
regexes = ['''4111111111\d{6}''', '''5555555555554444''']
```

gitleaks has no Luhn support, so leave card detection to Layer 1 and use
gitleaks for the fixed-shape identifiers.

### Actually blocking the merge

The checks above only advise until GitHub is told to require them:

**Settings → Branches → Add branch ruleset** for `main`:

- Require a pull request before merging
- **Require status checks to pass** → add `test` (and
  `no-personal-data-in-history` if you add Layer 3)
- Require branches to be up to date before merging
- Block force pushes — **add this after the force-push below, not before**

Without the required-status-check setting, a red check is just a red mark and
the merge button still works.

## Residual risk after the force-push

1. **Old objects survive on GitHub for a while.** Force-pushing makes the old
   commits unreachable, not absent: they stay retrievable by direct SHA URL until
   GitHub garbage-collects. For a private repo with few collaborators this is
   acceptable. To force the issue, ask GitHub Support to run `gc`.
2. **Every clone still holds the old history.** A `git pull` will merge it back
   in and undo this. Everyone must **delete their clone and re-clone.** Tell
   them before you push, not after.
3. **The mirror backup holds the original data in full.** That is what it is for.
   Keep it offline and delete it once you are satisfied — it is now the only
   copy, which cuts both ways.

## Reproducing the audit

```bash
# every blob reachable from every ref, dumped once and scanned
git rev-list --objects --all \
  | git cat-file --batch-check='%(objecttype) %(objectname) %(rest)' \
  | awk '$1=="blob" {print $2}' | sort -u > /tmp/blobs
while read -r h; do git cat-file blob "$h"; done < /tmp/blobs > /tmp/content
grep -nEo -e '\b[0-9]{13,19}\b' -e '[STFGM][0-9]{7}[A-Z]' /tmp/content \
  | cut -d: -f2- | sort | uniq -c | sort -rn
```

`git log --all -S'<value>'` finds the commits that added or removed any single
value.

## Backup

Full pre-rewrite mirror, containing every original value:

```
c:/Users/PC-user/Documents/Projects/Finstone/Pipeline-backup-20260813-102045.git
```

95 commits, taken immediately before the rewrite. Restore with
`git clone <that path> Pipeline-restored`.
