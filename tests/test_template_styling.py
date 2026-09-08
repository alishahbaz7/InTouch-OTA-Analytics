"""Every class a template uses must actually be styled.

This exists because of a real regression: a fallback filter shipped using `.filter-row`,
`.filter-group` and `.num`, none of which existed in the stylesheet. The page rendered, no test
failed, and the controls came out as raw blue browser links in the middle of a dark dashboard.
Nothing catches that except looking — or this.

It also pins the reuse: the fallback list must use the same controls the rest of the app does
rather than inventing a third convention on the same screen.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "ota_analytics" / "web"
CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
TEMPLATES = sorted((WEB / "templates").glob("*.html"))

# Classes that are legitimately not in the stylesheet.
ALLOWED = {
    # Applied by script or used purely as a hook, not for appearance.
    "js", "no-js",
    # Pre-existing and left alone deliberately: these were already in the templates before this
    # test, and restyling someone else's markup on a guess is worse than recording it. Listed
    # individually so the guard still catches anything NEW.
    "status-text",       # base.html
    "key",               # macros.html
    "inline-schedule",   # update.html
}


def literal_classes(markup: str) -> set[str]:
    """Class names written out in full, ignoring anything a template builds at render time.

    `class="pill pill-{{ status }}"` contributes `pill` and nothing else: the second half is
    only knowable at runtime, and guessing at it would make this test lie in both directions.
    """
    found: set[str] = set()
    for value in re.findall(r'class="([^"]*)"', markup):
        if "{%" in value:
            continue                      # a whole conditional class list; skip it
        # Drop {{ ... }} expressions, then take what is left as literal names.
        for name in re.sub(r"\{\{.*?\}\}", " ", value).split():
            if name.isidentifier() or re.fullmatch(r"[a-z0-9-]+", name):
                found.add(name)
    return found


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_class_a_template_uses_is_styled(template: Path):
    markup = template.read_text(encoding="utf-8")
    # A standalone page may carry its own <style> — the sign-in page does, because it renders
    # before anyone is allowed to see the dashboard's layout.
    inline = " ".join(re.findall(r"<style>(.*?)</style>", markup, re.S))

    used = literal_classes(markup) - ALLOWED
    missing = sorted(name for name in used
                     if f".{name}" not in CSS and f".{name}" not in inline)
    assert not missing, (
        f"{template.name} uses class(es) with no rule in app.css: {missing}. "
        "Either style them or use the existing ones — an unstyled class renders as a raw "
        "browser default in the middle of the dashboard."
    )


# ─── the fallback controls reuse what the app already has ───────────────────

CHANGES = (WEB / "templates" / "changes.html").read_text(encoding="utf-8")


def test_the_fallback_filters_use_the_shared_tab_control():
    """The same control the window selector at the top of this page uses."""
    assert CHANGES.count('class="window-tabs"') >= 2
    assert "Repeats only" in CHANGES


def test_the_fallback_headers_use_the_shared_sortable_macro():
    """Not a bespoke link: `th.sortable` / `.sorted` / `.sort-arrow` are already styled."""
    assert "m.sortable(" in CHANGES
    macros = (WEB / "templates" / "macros.html").read_text(encoding="utf-8")
    assert "{% macro sortable(" in macros
    assert 'class="sortable' in macros and "sort-arrow" in macros


def test_the_stylesheet_is_versioned():
    """An unversioned stylesheet is cached under a URL that never changes, so a restyled page
    keeps rendering with the old rules — indistinguishable from the new CSS being broken."""
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=" in base
