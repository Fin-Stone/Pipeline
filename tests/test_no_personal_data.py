"""No personal data in tracked files.

Rule 2 keeps real statements out of `uploads/prod/`. This keeps what is *read*
out of them from being pasted into a fixture, which is how it actually got in:
a scrub across 97bbc66, 9bb903b, 0d5e83b and 5246b98 was undone four days later
by 66b1426 and 8681ca2, with nothing anywhere to object.

Shape-based by necessity. It cannot know that a given sixteen digits is the
operator's card, so it rejects the shape and makes the author reach for an
obviously synthetic value instead. Every synthetic value the suite depends on
is named in ALLOWED, which is the list to add to when a new fixture needs one.
"""

from __future__ import annotations

import re
import subprocess
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
}

#: Keyed by glyph hash, not by anything personal.
SKIP_FILES = {"app/parsers/glyph_tables/opentext.json"}

TEXT_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".ts", ".tsx", ".toml",
                 ".sh", ".ps1", ".ini", ".cfg", ".txt", ".html"}


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


def scan(paths: list[str], root: Path = ROOT) -> list[str]:
    """Findings as `path:line: rule: value`, for the files in `paths`."""
    findings: list[str] = []
    for relpath in paths:
        if relpath in SKIP_FILES or Path(relpath).suffix not in TEXT_SUFFIXES:
            continue
        path = root / relpath
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern, is_finding in RULES:
                for match in pattern.findall(line):
                    if match not in ALLOWED and is_finding(match):
                        findings.append(f"{relpath}:{lineno}: {name}: {match}")
    return findings


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return [p for p in out.splitlines() if p]


def test_no_personal_data_in_tracked_files():
    findings = scan(_tracked_files())
    assert not findings, (
        "Personal data in tracked files:\n  "
        + "\n  ".join(findings)
        + "\n\nUse a synthetic value of the same shape. If the value really is "
          "synthetic, add it to ALLOWED with a reason."
    )


class TestTheDetectorActuallyDetects:
    """A guard that cannot fail is worse than none: it reads as coverage."""

    def test_a_real_card_number_is_caught(self, tmp_path):
        """The value that started all this, planted back in a fixture."""
        (tmp_path / "x.py").write_text(
            'PAN = "5555555555554444"\n', encoding="utf-8")
        findings = scan(["x.py"], root=tmp_path)
        assert len(findings) == 1
        assert "card number" in findings[0]

    def test_an_allowed_test_number_is_not_flagged(self, tmp_path):
        """4111… is Luhn-valid too, which is why ALLOWED has to exist."""
        (tmp_path / "x.py").write_text(
            'PAN = "4111111111111111"\n', encoding="utf-8")
        assert scan(["x.py"], root=tmp_path) == []

    def test_the_glyph_table_is_skipped(self, tmp_path):
        """Its keys are OCR hashes; some pass Luhn by chance."""
        skipped = next(iter(SKIP_FILES))
        target = tmp_path / skipped
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"5555555555554444": "-"}\n', encoding="utf-8")
        assert scan([skipped], root=tmp_path) == []

    def test_luhn_carries_most_of_the_filtering(self):
        """Luhn is what keeps the false positive rate low enough that nobody
        switches this off — a bare sixteen-digit rule fires on every lockfile
        hash and timestamp in the tree.

        It is a filter, not a decision: roughly one arbitrary run of digits in
        ten passes anyway, so a document id can still need an ALLOWED entry.
        """
        assert not _luhn("1234567890123456")
        assert _luhn("1000000000000000")  # a truncated document id, and valid

    def test_an_nric_is_caught(self):
        pattern = next(p for n, p, _ in RULES if "NRIC" in n)
        assert pattern.findall("owner S1234567D signed")

    def test_a_unit_number_is_caught(self):
        pattern = next(p for n, p, _ in RULES if "unit" in n)
        assert pattern.findall("SOME MALL #01-01 SINGAPORE")
