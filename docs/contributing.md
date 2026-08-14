# Contributing

Two repositories, one gate, and a normal pull request flow after it.

If you read one section, read [Why there are two repositories](#why-there-are-two-repositories).
Everything else follows from it.

---

## The shape of it

| Where | What lives there |
|---|---|
| `Fin-Stone/Pipeline-staging` — **private** | Where the work happens. Feature branches, commits, rewrites. |
| `Fin-Stone/Pipeline` — **public** | `dev`, `main`, releases, the published image, and feature branches once they have passed the gate. |

```
  feature branch (staging, private)
        │   push
        ▼
  ┌───────────────────┐   personal-data scan over the whole range
  │  publish.yml gate │   squashed to one commit if it passes
  └───────────────────┘
        │
        ▼
  feature branch (public) ──PR──▶ dev ──PR──▶ main ──tag──▶ release + image
                            │            │
                       pr-checks    regression
```

## Why there are two repositories

On a public repository **the push is the publication**. It completes before any
workflow starts, so nothing in CI can prevent it — CI can only tell you it has
already happened. And it cannot be taken back:

- deleting the branch leaves the commits fetchable by sha;
- opening a pull request creates a permanent public `refs/pull/N/head`;
- public push events, including commit subjects, are archived by third parties
  within seconds;
- fork networks share an object store, so anything forked persists across it.

Nor is there anything on GitHub that blocks a push based on file contents.
Push rulesets are private-repository-only and cannot read a file anyway; secret
scanning custom patterns are a paid add-on and are regex-only, so they cannot
run the Luhn check that keeps the card rule usable. Hosted GitLab and Bitbucket
are no better — server-side hooks there are self-managed or Data Center only.

So the check sits on the boundary *into* public, which is the one place it can
still say no.

**This is not hypothetical.** See [handoff.md](../handoff.md): a Luhn-valid card
number, an account number and a home address reached tracked files, four days
after a previous scrub had removed the same class of data, because nothing
anywhere objected. The gap was never care. It was enforcement.

## Working on a change

```bash
git clone git@github.com:Fin-Stone/Pipeline-staging.git
cd Pipeline-staging
./bootstrap.sh          # or ./bootstrap.ps1 — this also installs the git hooks
git switch -c feature/read-the-interest-line
```

Push when you are ready. The gate scans every commit in the range, squashes the
branch to a single commit, and publishes that to the public repository. Then
open a pull request from the published branch into `dev`.

### Never push to the public repository directly

Your clone should have the public remote fetch-only:

```bash
git remote set-url --push public DISABLED
```

You still need `git fetch public` to pick up contributors' pull requests. You
do not need to push there, and a push that skips the gate is the one path this
design does not cover.

## The hooks

`bootstrap` sets `core.hooksPath` to `.githooks/`. Two hooks:

- **`commit-msg`** — the commit subject must be a conventional commit.
- **`pre-push`** — refuses to push personal data, scanning the whole range
  being pushed rather than the tip.

`pre-push` scans the range and not just the current files on purpose. A value
that arrived in one commit and left in the next is still in the history you are
about to send, and that is exactly what survived the earlier scrubs.

`git push --no-verify` skips the hook. It is documented here rather than hidden
because pretending otherwise would be false comfort — git always allows it. The
hook is fast feedback; the gate in `publish.yml` is the boundary, and nothing
local can skip that one.

### When the check fires

It prints `path:line: rule: value` for everything it found.

**Deleting the value in a new commit does not fix it.** The old commit still
carries it, and pushing sends both. Rewrite instead:

```bash
git rebase -i <commit-before-the-bad-one>   # edit or drop the commit
# or, if it is the most recent commit:
git commit --amend
```

Feature branches carry no protection, so force-pushing your own is safe.

If the value really is synthetic, add it to `ALLOWED` in
`tests/test_no_personal_data.py` **with a reason beside it**. That list is the
honest record of what is deliberately fake, and every entry earning its place
is what keeps the check trustworthy enough to leave switched on.

If it reached the public repository, stop. Per Rule 2 that is a data incident,
not a cleanup commit — say so plainly and see `handoff.md` for what a response
looks like.

### You almost never need to commit a real document

`finstone doctor <path>` and `finstone quarantine` exist so a failing statement
never has to be shared to diagnose a parser bug, and `--redact` masks
filenames, descriptions and references while keeping the amounts and structure.
Reach for those first.

## Commit messages

```
<type>[optional scope][!]: <subject>
```

Types: `feat`, `fix`, `patch`, `chore`, `docs`, `refactor`, `test`, `ci`,
`build`, `perf`, `revert`. Subject is 72 characters or fewer.

A breaking change is a `!` after the type (`feat!:`) or a `BREAKING CHANGE:`
footer. `breaking` is not itself a type.

The grammar lives in `infra/scripts/check-commit-msg.sh` — one copy, used by
the hook, by CI and by the pull request title check.

### The prose goes in the body

This repository's history is full of subjects like *"Keep a wrapped description
with the row that printed it"*, which say more than `feat(ui): add dashboard`
ever will. That writing is worth keeping. It moves to the commit **body**,
where there is more room for it and nothing is truncating it at 72 characters:

```
fix(parsers): keep a wrapped description with the row that printed it

A description that wraps across two lines was being attached to whichever
row followed it, which put the merchant on the next transaction and left
this one blank...
```

The subject is machine-readable because the version bump is computed from it.
The body is for the next person.

### The pull request title is the one that matters

`dev` takes squash merges, so **the pull request title becomes the commit
subject** that lands in the branch history. It is checked for the same reason.

## Merging

| | Method | Why |
|---|---|---|
| feature → `dev` | **Squash** | One commit per change. The branch's own commits were already squashed at publish time. |
| `dev` → `main` | **Merge commit** | Keeps the two branches sharing history. |

**Do not squash `dev` into `main`.** A squash creates a new commit that exists
on `main` and nowhere in `dev`'s history, so the branches diverge permanently
and every later release pull request shows conflicts that are not real. The
rulesets enforce this by restricting the allowed merge method per branch, but
it is worth knowing why the button is missing.

## What runs where

| Workflow | Trigger | What it does |
|---|---|---|
| `publish` (staging) | push to a feature branch | The gate. Scans, squashes, publishes. |
| `pr-checks` | PR into `dev` | ruff, `tsc --noEmit`, SQLite-only pytest, commit grammar, personal data, pip-audit + gitleaks. |
| `regression` | PR into `main`, push to `dev`/`main` | The suite on **both** engines, the client build, and the branch guard. |
| `pr-agent` | PR into `dev` | Automated review. Advisory — not a required check. |
| `release` | push to `main` | Tags and releases when the version changed. |
| `image` | push to `main`, and dispatched by `release` on the new tag | Builds, asserts no real data is in the image, publishes to GHCR. |

The dual-engine run in `regression` is the enforcement mechanism for Rule 1: a
Postgres-ism fails the SQLite job and a SQLite assumption fails the Postgres
one. Two real bugs reached the ledger before it existed and both were invisible
on a single engine.

## Running the checks locally

```bash
ruff check .                                   # same pinned version as CI
python -m pytest -q                            # SQLite only, seconds
python tests/test_no_personal_data.py origin/dev..HEAD
cd ui/pwa && npx tsc --noEmit
```

For the Postgres half, set `TEST_DATABASE_URL` and run the suite again — the
engine list in `tests/conftest.py` is derived from that variable, which is why
leaving it unset gives you the fast run.

## Releasing

1. On `dev`, ask what the commits imply:

   ```bash
   ./infra/scripts/next-version.sh
   ```

2. Commit the bump as `chore(release): v0.2.0`, updating `pyproject.toml`,
   `ui/pwa/package.json` (they ship as one artifact, so they move together) and
   a new `CHANGELOG.md` section.
3. Open the `dev` → `main` pull request. Merge it with a merge commit.

`release.yml` then tags and publishes. It recomputes the version from the
commits and **fails rather than tagging** if it disagrees with what you wrote —
a release whose number contradicts its own changelog is worse than a failed
build, because it ships.

Its last step dispatches `image.yml` on the tag it just pushed, which is the
only way that build ever sees a tag: GitHub does not start workflow runs from
events raised by `GITHUB_TOKEN`, so a `tags:` trigger on `image.yml` would
never fire, and for v0.1.0 it did not. So a release builds the image twice from
the same commit — once from the push to `main`, which moves `latest`, and once
from the tag, which is what produces `0.2.0` and `0.2`. The second build reuses
the first's cache.

To publish an image for a tag that missed one, dispatch `image` manually and
pick that tag under **Run workflow → Use workflow from**.

Below 1.0 the minor is the compatibility signal: a breaking change and a
feature both move it, and the changelog is where the difference is recorded.

## The limits of the automated checks

Worth stating plainly, so a green tick is not read as more than it is.

The scanner knows **shapes** — card numbers that pass Luhn, NRIC, UEN, unit
numbers, phone numbers. It cannot know whose data a value is, and it will never
catch a real merchant name, a surname, or a genuine transaction description.
Those are the reviewer's job, and the pull request template asks about them for
that reason.

A contributor's pull request also arrives on the public repository without
passing the staging gate — inherent to public open-source intake, on any host.
`pr-checks / privacy` scans it, but by then it is already published.
