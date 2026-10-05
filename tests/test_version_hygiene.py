"""One version number, declared once, and the literature kept level with it.

Every one of these is here because the alternative is remembering, and remembering failed: a
release went out with the changelog and the executables correct while the number the user
actually reads had not moved. A checklist item gets skipped; a failing test does not.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import ota_analytics
from ota_analytics import __version__

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_the_version_is_a_version():
    assert SEMVER.match(__version__), __version__


def test_the_version_history_leads_with_the_current_version():
    """The in-app history is what a screenshot identifies itself by, so it cannot lag."""
    newest = ota_analytics.VERSION_HISTORY[0]
    assert newest[0] == __version__, (
        f"__version__ is {__version__} but VERSION_HISTORY starts at {newest[0]} — "
        "the version note shown in the UI would describe the wrong release.")


def test_the_release_date_matches_the_history_entry():
    assert ota_analytics.RELEASED == ota_analytics.VERSION_HISTORY[0][1]


def test_no_entry_still_calls_itself_in_progress():
    """A shipped version describing itself as unfinished is a defect in the shipped UI."""
    stale = [v for v, _, note in ota_analytics.VERSION_HISTORY
             if note.lower().startswith("in progress")]
    assert not stale, f"released versions still marked 'In progress': {stale}"


def test_the_changelog_documents_this_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {__version__}" in text, (
        f"CHANGELOG.md has no section for {__version__}. The version history gives one line; "
        "the changelog is where the reasoning lives.")


# ─── one number, one place ──────────────────────────────────────────────────

def test_the_build_derives_the_version_rather_than_repeating_it():
    """The number on the file must not be able to drift from the number in the code."""
    build = (ROOT / "build.py").read_text(encoding="utf-8")
    assert "from ota_analytics import __version__" in build


def test_nothing_else_declares_a_version():
    """One version for the whole application — not one for the UI and another for the build.

    Only assignments are checked. Prose that mentions an old release ("withdrawn in 1.2.1") is
    history, not a declaration, and test fixtures use version-shaped strings as firmware.
    """
    declaration = re.compile(
        r"""(?:^|\s)(?:__version__|VERSION|version)\s*[=:]\s*["']\d+\.\d+\.\d+["']""")

    offenders = []
    for path in ROOT.rglob("*"):
        if path.suffix not in {".py", ".html", ".json", ".toml", ".cfg", ".spec"}:
            continue
        if any(part in {".venv", "dist", "build", ".git", "tests"} for part in path.parts):
            continue
        if path.name == "__init__.py" and path.parent.name == "ota_analytics":
            continue                      # the one place it is allowed to live
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if declaration.search(text):
            offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, (
        f"a second version is declared in: {offenders}. Derive it from "
        "ota_analytics.__version__ instead — two numbers drift, and the one that lags makes a "
        "bug report point at the wrong code.")


# ─── the branch is named for the version ────────────────────────────────────

def current_branch() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def test_a_version_branch_is_named_for_the_version_it_ships():
    """Work happens on a branch named exactly the version, and never on main.

    Skipped elsewhere — a detached checkout, a CI clone or someone's scratch branch is not a
    rule violation, and failing there would only teach people to ignore this.
    """
    branch = current_branch()
    if branch is None or not SEMVER.match(branch):
        pytest.skip(f"not on a version branch (on {branch!r})")
    assert branch == __version__, (
        f"branch {branch} but __version__ is {__version__} — one of the two is wrong.")
