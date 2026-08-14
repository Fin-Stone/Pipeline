# Changelog

Notable changes per release. `release.yml` reads the section matching the
version in `pyproject.toml` and uses it as the GitHub release notes, so the
heading format matters: `## <version>`, nothing else on the line.

Below 1.0 a breaking change and a feature both move the minor version. Which
one it was is recorded here rather than in the number.

## Unreleased

### Added

- A two-repository workflow: work happens in a private staging repository and
  reaches the public one only through a gate that scans every commit being
  published for personal data. See [docs/contributing.md](docs/contributing.md).
- `scan_blobs` and `scan_messages` in `tests/test_no_personal_data.py`, which
  scan a revision range rather than the working tree. A value added in one
  commit and removed in the next is invisible to a tree scan, which is how the
  earlier scrubs were undone.
- Git hooks (`.githooks/`), installed by `bootstrap`: `pre-push` refuses to
  push personal data, `commit-msg` enforces conventional commit subjects.
- Conventional commits, with one grammar shared by the hook, CI and the pull
  request title check (`infra/scripts/check-commit-msg.sh`).
- Semantic versioning driven by commit types
  (`infra/scripts/next-version.sh`), with `release.yml` tagging `main` and
  refusing to tag a version the commits do not imply.
- `pr-checks` on the way into `dev`: ruff, TypeScript typecheck, the SQLite
  suite, commit grammar, personal data, pip-audit and gitleaks.
- Automated pull request review via PR-Agent, advisory rather than required.
- An assertion in `image.yml` that no real data is in the published image,
  rather than trusting `.dockerignore` to have stayed correct.

### Changed

- `test.yml` is now `regression.yml`, runs on pushes to `dev` as well as
  `main`, and refuses pull requests into `main` from anywhere but `dev` or a
  `hotfix/*` branch.
- ruff runs over the codebase for the first time, with a deliberately narrow
  rule set. `E501` is excluded so the hand-wrapped commentary survives, and
  `B905` and `UP` are deferred to their own commits.
