"""No personal data in tracked files, or in the history being published.

Rule 2 keeps real statements out of `uploads/prod/`. This keeps what is *read*
out of them from being pasted into a fixture, which is how it actually got in:
a scrub across 97bbc66, 9bb903b, 0d5e83b and 5246b98 was undone four days later
by 66b1426 and 8681ca2, with nothing anywhere to object.

Shape-based by necessity. It cannot know that a given sixteen digits is the
operator's card, so it rejects the shape and makes the author reach for an
obviously synthetic value instead. Every synthetic value the suite depends on
is named in ALLOWED, which is the list to add to when a new fixture needs one.

Three entry points, one rule table:

- `scan` looks at the working tree, and is what the test below asserts on.
- `scan_blobs` looks at every blob a revision range introduces. A tree scan
  cannot see a value that arrived in one commit and left in the next, which is
  exactly what survived the earlier scrubs — the tip was clean and `git log -S`
  still found them.
- `scan_messages` looks at the commit messages themselves. A subject naming a
  real merchant is as published as a line of code, and more so on a public
  repository, where push events carry subjects into third-party archives
  within seconds.

`scan_blobs` and `scan_messages` are what the pre-push hook and the publish
gate call. They take a range rather than a tree because the unit being
published is a range.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Values that are correct to have in the repository. Anything added here needs
#: a reason beside it.
ALLOWED = {
    "4111111111111111", "4111111111112222",   # standard Visa test numbers
    "4111111111113333", "4111111111114321",
    "5555555555554444",                        # standard Mastercard test number
    "T00XX0000X",                              # synthetic UEN in the Trust wrap fixtures
    "#01-01",                                  # synthetic unit number, same fixtures
    # The textbook placeholder NRIC, sequential digits. It is in this file's
    # own history: the first version of this check asserted against it as a
    # literal, and 2f2485c stopped writing values down at the tip. The range
    # scan still reaches that blob, and blocking every push on the detector's
    # own retired fixture is how a check ends up switched off.
    "S1234567D",
}

#: Keyed by glyph hash, not by anything personal.
SKIP_FILES = {"app/parsers/glyph_tables/opentext.json"}

#: `""` covers extensionless files — the Dockerfile and the hooks in
#: `.githooks/`, which are shell scripts git sees as having no suffix. Without
#: it a hook could carry a pasted value and never be looked at.
TEXT_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".ts", ".tsx", ".toml",
                 ".sh", ".ps1", ".ini", ".cfg", ".txt", ".html", ""}


def _luhn(number: str) -> bool:
    digits = [int(c) for c in reversed(number)]
    total = sum(digits[0::2])
    for d in digits[1::2]:
        d *= 2
        total += d - 9 if d > 9 else d
    return total % 10 == 0


#: (name, pattern, extra predicate). A match is a finding when it is not in
#: ALLOWED and the predicate agrees.
RULES = [
    ("card number (Luhn-valid)", re.compile(r"\b\d{13,19}\b"), _luhn),
    ("Singapore NRIC/FIN", re.compile(r"\b[STFGM]\d{7}[A-Z]\b"), lambda m: True),
    ("Singapore UEN", re.compile(r"\b[T-Z]\d{2}[A-Z]{2}\d{4}[A-Z]\b"), lambda m: True),
    ("unit number", re.compile(r"#\d{2}-\d{2,4}\b"), lambda m: True),
    ("Singapore phone", re.compile(r"\+65[ -]?\d{4}[ -]?\d{4}\b"), lambda m: True),
]


def _git(args: list[str], root: Path) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, check=True).stdout


def _scan_text(label: str, text: str) -> list[str]:
    """Findings in one blob of text, as `label:line: rule: value`.

    The single place the rules are applied. Everything below is about deciding
    what text to hand it.
    """
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pattern, is_finding in RULES:
            for match in pattern.findall(line):
                if match not in ALLOWED and is_finding(match):
                    findings.append(f"{label}:{lineno}: {name}: {match}")
    return findings


def _is_scannable(relpath: str) -> bool:
    return relpath not in SKIP_FILES and Path(relpath).suffix in TEXT_SUFFIXES


def scan(paths: list[str], root: Path = ROOT) -> list[str]:
    """Findings as `path:line: rule: value`, for the files in `paths`."""
    findings: list[str] = []
    for relpath in paths:
        if not _is_scannable(relpath):
            continue
        path = root / relpath
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(_scan_text(relpath, text))
    return findings


def _batch(args: list[str], shas: list[str], root: Path) -> bytes:
    return subprocess.run(
        ["git", "cat-file", *args], cwd=root, check=True,
        input=("\n".join(shas) + "\n").encode(), capture_output=True).stdout


def _only_blobs(shas: list[str], root: Path) -> list[str]:
    """Of these object ids, the ones that are blobs.

    `rev-list --objects` reports trees with a path too, and a directory has no
    suffix, so the scannable-path filter lets them through. Asking `--batch`
    for a tree and then skipping it is where this first went wrong: the header
    was stepped over and the body was not, so every object after it was read at
    the wrong offset and findings were reported against the wrong file. Types
    come from `--batch-check`, which emits one line per object and no bodies at
    all, so there is nothing to misalign.
    """
    if not shas:
        return []
    out = _batch(["--batch-check"], shas, root).decode("utf-8", "replace")
    return [parts[0] for line in out.splitlines()
            if len(parts := line.split()) >= 3 and parts[1] == "blob"]


def _cat_blobs(shas: list[str], root: Path) -> dict[str, str]:
    """Contents of many blobs in one `git cat-file` call.

    One subprocess per blob is the obvious implementation and is too slow to
    live in a pre-push hook: scanning this repository's history means thousands
    of objects. `--batch` reads a sha per line and writes
    `<sha> <type> <size>\\n<content>\\n` back, so the output has to be walked by
    length rather than split on anything — content contains newlines.
    """
    blobs = _only_blobs(shas, root)
    if not blobs:
        return {}
    out = _batch(["--batch"], blobs, root)

    contents: dict[str, str] = {}
    pos = 0
    for sha in blobs:
        newline = out.find(b"\n", pos)
        if newline == -1:
            break
        header = out[pos:newline].decode("utf-8", "replace").split()
        if len(header) < 3:
            # `<sha> missing`, the one case with no body to step over.
            pos = newline + 1
            continue
        size = int(header[2])
        start = newline + 1
        contents[sha] = out[start:start + size].decode("utf-8", "replace")
        pos = start + size + 1      # +1 for the newline git appends
    return contents


def scan_blobs(*rev_args: str, root: Path = ROOT) -> list[str]:
    """Findings in every blob a revision range introduces.

    Takes rev-list arguments rather than one string, because pushing a branch
    the remote has never seen is expressed as `<sha> --not --remotes=origin` —
    everything this push adds, rather than the whole history behind it.

    `rev-list --objects` rather than a walk of per-commit diffs: it yields
    everything reachable from one end and not the other in a single call, which
    is both faster and correct across merge commits, where `diff-tree` shows
    nothing by default and a conflict resolution could otherwise slip through.

    Deduplicating by sha matters more than it looks — an unchanged file appears
    once per commit that carries it, and the same blob only needs reading once.
    """
    listing = _git(["rev-list", "--objects", *rev_args], root)

    by_sha: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _, relpath = line.partition(" ")
        # Commits and the root tree have no path; trees have one but no
        # suffix we care about, so the filter drops them too.
        if relpath and _is_scannable(relpath):
            by_sha.setdefault(sha, relpath)

    shas = list(by_sha)
    findings: list[str] = []
    for sha, text in _cat_blobs(shas, root).items():
        findings.extend(_scan_text(f"{by_sha[sha]} ({sha[:8]})", text))
    return findings


def scan_messages(*rev_args: str, root: Path = ROOT) -> list[str]:
    """Findings in the commit messages of a revision range.

    9bb903b — "Illustrate with invented merchants, not this household's" — is
    the reason this exists: the message channel carries the same data as the
    diff, and on a public repository it travels further, faster.
    """
    # Unit and record separators, so a message containing either is not a
    # realistic concern while newlines are.
    raw = _git(["log", "--format=%H%x1f%B%x1e", *rev_args], root)

    findings: list[str] = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, message = record.partition("\x1f")
        findings.extend(_scan_text(f"{sha[:8]} (commit message)", message))
    return findings


def _candidate_files() -> list[str]:
    """Tracked files, plus untracked ones git would not ignore.

    The untracked half matters: `git ls-files` alone does not see a file that
    has never been added, so a new fixture full of statement data reads as clean
    until the moment it is committed. This check was written with that gap in it
    and did not scan itself.
    """
    tracked = _git(["ls-files"], ROOT).splitlines()
    untracked = _git(["ls-files", "--others", "--exclude-standard"], ROOT).splitlines()
    return [p for p in [*tracked, *untracked] if p]


def _with_check_digit(prefix: str) -> str:
    """`prefix` plus the digit that makes it pass Luhn.

    Built at runtime because this file is scanned like any other: a card-shaped
    literal written here to prove the detector works would be a finding, and a
    *real* one would be the exact mistake being guarded against.
    """
    for digit in "0123456789":
        if _luhn(prefix + digit):
            return prefix + digit
    raise AssertionError("unreachable: one of ten digits must satisfy Luhn")


def test_no_personal_data_in_tracked_files():
    findings = scan(_candidate_files())
    assert not findings, (
        "Personal data in tracked files:\n  "
        + "\n  ".join(findings)
        + "\n\nUse a synthetic value of the same shape. If the value really is "
          "synthetic, add it to ALLOWED with a reason."
    )


class TestTheDetectorActuallyDetects:
    """A guard that cannot fail is worse than none: it reads as coverage."""

    def test_a_card_shaped_number_is_caught(self, tmp_path):
        """Assembled here rather than written down, for the reason in
        `_with_check_digit`."""
        pan = _with_check_digit("540012000000000")
        (tmp_path / "x.py").write_text(f'PAN = "{pan}"\n', encoding="utf-8")
        findings = scan(["x.py"], root=tmp_path)
        assert len(findings) == 1
        assert "card number" in findings[0]

    def test_an_allowed_test_number_is_not_flagged(self, tmp_path):
        """The standard test numbers are Luhn-valid too, which is why ALLOWED
        has to exist at all."""
        allowed_pan = next(v for v in ALLOWED if v.isdigit() and len(v) == 16)
        (tmp_path / "x.py").write_text(
            f'PAN = "{allowed_pan}"\n', encoding="utf-8")
        assert scan(["x.py"], root=tmp_path) == []

    def test_the_glyph_table_is_skipped(self, tmp_path):
        """Its keys are OCR hashes; some pass Luhn by chance."""
        skipped = next(iter(SKIP_FILES))
        target = tmp_path / skipped
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            '{"%s": "-"}\n' % _with_check_digit("540012000000000"),
            encoding="utf-8")
        assert scan([skipped], root=tmp_path) == []

    def test_an_untracked_file_is_still_scanned(self, tmp_path):
        """The gap this check originally shipped with: a brand new fixture is
        not tracked yet, so `git ls-files` cannot see it."""
        (tmp_path / "new.py").write_text(
            f'PAN = "{_with_check_digit("540012000000000")}"\n', encoding="utf-8")
        assert scan(["new.py"], root=tmp_path)

    def test_luhn_carries_most_of_the_filtering(self):
        """Luhn is what keeps the false positive rate low enough that nobody
        switches this off — a bare sixteen-digit rule fires on every lockfile
        hash and timestamp in the tree.

        It is a filter, not a decision: roughly one arbitrary run of digits in
        ten passes anyway, so a document id can still need an ALLOWED entry.
        """
        assert not _luhn("1234567890123456")
        assert sum(_luhn(f"{n:016d}") for n in range(1000)) > 50

    def test_an_nric_is_caught(self):
        pattern = next(p for n, p, _ in RULES if "NRIC" in n)
        nric = "S" + "1234567" + "D"          # assembled: this file is scanned
        assert pattern.findall(f"owner {nric} signed")

    def test_a_unit_number_is_caught(self):
        pattern = next(p for n, p, _ in RULES if "unit" in n)
        unit = "#" + "07-02"
        assert pattern.findall(f"SOME MALL {unit} SINGAPORE")


def _init_repo(path: Path) -> None:
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _commit(path: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path,
                   check=True, capture_output=True)


class TestTheRangeScanSeesWhatTheTreeScanCannot:
    """The failure that motivated `scan_blobs`.

    Section B of the handoff: four commits removed personal data from the
    working tree and every one of the values stayed a `git log -S` away. A
    check that only ever looks at the tip reports those commits as a clean-up
    and the repository as clean.
    """

    def test_a_value_added_then_removed_is_still_found(self, tmp_path):
        _init_repo(tmp_path)
        (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
        _commit(tmp_path, "seed")
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                              check=True, capture_output=True, text=True).stdout.strip()

        pan = _with_check_digit("540012000000000")
        (tmp_path / "fixture.py").write_text(f'PAN = "{pan}"\n', encoding="utf-8")
        _commit(tmp_path, "add a fixture")
        (tmp_path / "fixture.py").write_text('PAN = "REDACTED"\n', encoding="utf-8")
        _commit(tmp_path, "take it back out")

        # The tip is clean, which is exactly why the tree scan is not enough.
        assert scan(["fixture.py", "seed.py"], root=tmp_path) == []

        findings = scan_blobs(f"{base}..HEAD", root=tmp_path)
        assert len(findings) == 1
        assert "card number" in findings[0]
        assert "fixture.py" in findings[0]

    def test_a_clean_range_is_clean(self, tmp_path):
        _init_repo(tmp_path)
        (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
        _commit(tmp_path, "seed")
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                              check=True, capture_output=True, text=True).stdout.strip()
        (tmp_path / "seed.py").write_text("x = 2\n", encoding="utf-8")
        _commit(tmp_path, "change it")
        assert scan_blobs(f"{base}..HEAD", root=tmp_path) == []

    def test_the_glyph_table_is_skipped_in_a_range_too(self, tmp_path):
        """The skip list has to hold on both paths, or the range scan becomes
        the thing that makes people switch the check off."""
        _init_repo(tmp_path)
        (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
        _commit(tmp_path, "seed")
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                              check=True, capture_output=True, text=True).stdout.strip()

        skipped = next(iter(SKIP_FILES))
        target = tmp_path / skipped
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"%s": "-"}\n' % _with_check_digit("540012000000000"),
                          encoding="utf-8")
        _commit(tmp_path, "add glyph table")
        assert scan_blobs(f"{base}..HEAD", root=tmp_path) == []

    def test_a_value_in_a_commit_message_is_caught(self, tmp_path):
        """The diff can be spotless and the subject still publish the value."""
        _init_repo(tmp_path)
        (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
        _commit(tmp_path, "seed")
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                              check=True, capture_output=True, text=True).stdout.strip()

        (tmp_path / "seed.py").write_text("x = 2\n", encoding="utf-8")
        _commit(tmp_path, f"drop the card {_with_check_digit('540012000000000')}")

        assert scan_blobs(f"{base}..HEAD", root=tmp_path) == []
        findings = scan_messages(f"{base}..HEAD", root=tmp_path)
        assert len(findings) == 1
        assert "commit message" in findings[0]


def main(argv: list[str]) -> int:
    """Scan a revision range, or the working tree. The entry point the hook
    and the gate both use.

        python tests/test_no_personal_data.py origin/dev..HEAD
        python tests/test_no_personal_data.py --tree

    A test file with a `__main__` rather than a second script next to it: the
    rules, the allowlist and the skip list are what must not fork, and the
    surest way to keep one copy of them is to have one file.

    `--tree` exists because `pytest` is not a way to run this outside a
    developer's environment. Collecting this file means importing
    tests/conftest.py, which imports `app`, which imports SQLAlchemy — so a
    gate that installs nothing (deliberately, since everything here is standard
    library) cannot reach the assertion at all. It fails loading conftest, four
    seconds in, having checked nothing.
    """
    if not argv:
        print("usage: test_no_personal_data.py <rev-list arguments> | --tree",
              file=sys.stderr)
        return 2

    if argv[0] == "--tree":
        rev_range = "the working tree"
        findings = scan(_candidate_files())
    else:
        rev_range = " ".join(argv)
        findings = scan_blobs(*argv) + scan_messages(*argv)

    if not findings:
        print(f"no personal data in {rev_range}")
        return 0

    print(f"\nPersonal data in {rev_range}:\n", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(
        "\nThis must not reach the public repository. Rewrite the commits that"
        "\ncarry it — `git rebase -i` or `git commit --amend` — rather than"
        "\nadding a commit that removes it, which leaves it in the history."
        "\n\nIf the value really is synthetic, add it to ALLOWED with a reason."
        "\nSee docs/contributing.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
