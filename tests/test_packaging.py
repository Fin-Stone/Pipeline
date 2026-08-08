"""What the built distribution carries, as opposed to what the checkout does.

This exists because of a failure that no other test here could see. The glyph
table — the alphabet for statements drawn as bitmaps rather than typed — lives
in a directory with no `__init__.py`, so `packages.find` never collected it and
it was absent from every wheel and every image built from them.

Nothing errored. An empty alphabet decodes a statement to nothing at all, the
adapter's signature then matches no line on a page that has no lines, and the
document is reported as an unknown format. The published image could not read a
statement the same code read correctly from a source checkout, and the whole
test suite passed on both engines throughout.

So the rule is: **a file the code reads at runtime must be declared as package
data.** Checked statically here rather than by building a wheel, so it costs
milliseconds and runs on every commit.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "app"

#: Not shipped and not meant to be.
IGNORED = {"__pycache__", ".pytest_cache"}

#: Files that are not runtime data and so need not travel in the wheel.
#: `.md` is documentation sitting beside code for the reader's benefit;
#: `.mako` is Alembic's template, read only when a migration is *written*.
#: Everything else in a non-package directory is assumed to be something the
#: code opens, because that assumption failing silently is what this file is for.
NOT_RUNTIME_DATA = {".md", ".mako", ".pyc", ".pyo"}


def _declared() -> dict[str, list[str]]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config.get("tool", {}).get("setuptools", {}).get("package-data", {})


def _data_directories() -> list[Path]:
    """Directories inside the package that hold data and are not packages.

    A directory with an `__init__.py` is collected by `packages.find`; one
    without is invisible to it, and that is exactly the trap.
    """
    found = []
    for path in sorted(PACKAGE.rglob("*")):
        if not path.is_dir() or path.name in IGNORED:
            continue
        if (path / "__init__.py").exists():
            continue
        if any(
            child.is_file()
            and child.suffix != ".py"
            and child.suffix not in NOT_RUNTIME_DATA
            for child in path.iterdir()
        ):
            found.append(path)
    return found


class TestRuntimeDataIsShipped:
    def test_every_data_directory_is_declared(self):
        declared = _declared()
        covered = {
            (package.replace(".", "/") + "/" + pattern).rsplit("/", 1)[0]
            for package, patterns in declared.items()
            for pattern in patterns
        }
        missing = [
            str(path.relative_to(REPO_ROOT))
            for path in _data_directories()
            if str(path.relative_to(REPO_ROOT)).replace("\\", "/") not in covered
        ]
        assert not missing, (
            "these hold files the code reads at runtime but are not declared in "
            f"[tool.setuptools.package-data], so no wheel will contain them: {missing}"
        )

    def test_the_glyph_alphabet_is_one_of_them(self):
        """The specific case that went wrong, named so it cannot quietly leave."""
        from app.parsers import glyphs

        assert glyphs.TABLE_DIR.is_dir()
        assert any(glyphs.TABLE_DIR.glob("*.json"))

    def test_the_alphabet_loads(self):
        """An empty table is indistinguishable from a document with no text —
        which is why its absence was reported as an unknown format rather than
        as anything to do with a missing file."""
        from app.parsers import glyphs

        assert len(glyphs.load_tables()) > 300


class TestTheAdapterAndItsDataTravelTogether:
    def test_an_adapter_needing_glyphs_has_them(self):
        """`hsbc.cc` is registered from Python that ships regardless. Its
        alphabet is data that has to be asked for separately, and an adapter
        present without its data is worse than one that is absent: it claims a
        format it cannot read."""
        from app.parsers import glyphs
        from app.parsers.registry import build_default_registry

        registry = build_default_registry(None)
        names = {registration.adapter.name for registration in registry.registrations()}
        if "hsbc.cc" not in names:
            pytest.skip("hsbc.cc is not registered in this build")
        assert len(glyphs.load_tables()) > 0, \
            "hsbc.cc is registered but its alphabet is empty — it will read nothing"
