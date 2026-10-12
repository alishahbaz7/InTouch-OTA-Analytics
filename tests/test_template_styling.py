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
    """Not a bespoke link — and drawn like the data grid's own headers: the chevron on the sorted
    column (12-10-2026), never the old ▼ glyph."""
    assert "m.sortable(" in CHANGES
    macros = (WEB / "templates" / "macros.html").read_text(encoding="utf-8")
    assert "{% macro sortable(" in macros
    assert 'class="sortable' in macros and "sort-ind" in macros and "is-sorted" in macros
    for path in (WEB / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "sort-arrow" not in text and "▼" not in text and "▲" not in text, path.name


def test_the_stylesheet_is_versioned():
    """An unversioned stylesheet is cached under a URL that never changes, so a restyled page
    keeps rendering with the old rules — indistinguishable from the new CSS being broken."""
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=" in base


def test_no_rule_names_the_same_selector_twice():
    """Retiring a rule by pattern once cut half of a shared one, leaving
    `.console-list, .console-thread,` glued onto the next rule: valid CSS, silently different —
    both panes lost their frame. That leaves a selector named twice in one rule."""
    import re

    css = (WEB / "static" / "app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selectors in re.findall(r"(?:^|})\s*([^{}@]+?)\s*{", css):
        names = [s.strip() for s in selectors.split(",") if s.strip()]
        repeated = {n for n in names if names.count(n) > 1}
        assert not repeated, f"selector named twice in one rule: {sorted(repeated)}"


def test_every_rule_in_the_stylesheet_is_whole():
    """Retiring rules line by line once left half of a two-line rule behind — a stray
    `text-align: …; }` that the browser reads as the start of a selector, silently dropping the
    rule after it. Braces that do not balance are how that shows."""
    depth = 0
    for number, line in enumerate(CSS.splitlines(), start=1):
        depth += line.count("{") - line.count("}")
        assert depth >= 0, f"app.css line {number} closes a rule that was never opened"
    assert depth == 0, "app.css ends inside an open rule"


def test_cota_headings_carry_a_count_at_most_never_a_sentence():
    """The user's rule (2026-10-08): no help text printed beside a title — it reads as filler.
    A heading may carry a count; anything that explains goes in a data-tip, shown on hover."""
    allowed = {"of", "device", "devices", "call", "calls", "matching", "job", "see", "jobs"}
    for path in sorted((WEB / "templates").glob("*cota*.html")):
        text = path.read_text(encoding="utf-8")
        for h2 in re.findall(r"<h2[^>]*>(.*?)</h2>", text, re.S):
            for hint in re.findall(r'<span class="hint">(.*?)</span>', h2, re.S):
                prose = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|<[^>]+>", " ", hint, flags=re.S)
                words = {w.lower() for w in re.findall(r"[A-Za-z]+", prose)}
                assert words <= allowed, f"{path.name}: heading hint reads as help text: {sorted(words - allowed)}"


def test_no_heading_in_any_template_carries_help_text():
    """The same rule for every page outside COTA (Web FOTA, Update data, the first-run page):
    a heading carries its title and at most a count or a figure. "newest first", "a slice
    concentrating above its share of the fleet is a lead, not a cause" and the like go in the
    title's data-tip. A hint may itself carry a data-tip, so attributes on it are allowed."""
    allowed = {
        "of", "device", "devices", "models", "versions", "snapshots", "occurrences",
        "pending", "moved", "recorded", "shown",
        "past", "h", "peak", "at",      # "past 24h · N devices · peak N at 14:00"
        "detail",                       # the "detail →" link, an action rather than prose
    }
    checked = 0
    for path in sorted((WEB / "templates").glob("*.html")):
        if "cota" in path.name:
            continue                    # held by test_cota_headings_carry_a_count_at_most_never_a_sentence
        text = path.read_text(encoding="utf-8")
        for _, heading in re.findall(r"<h([23])[^>]*>(.*?)</h\1>", text, re.S):
            for hint in re.findall(r'<span class="hint"[^>]*>(.*?)</span>', heading, re.S):
                checked += 1
                prose = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|<[^>]+>", " ", hint, flags=re.S)
                words = {w.lower() for w in re.findall(r"[A-Za-z]+", prose)}
                assert words <= allowed, f"{path.name}: heading hint reads as help text: {sorted(words - allowed)}"
    assert checked, "no heading hints found; the pattern no longer matches the templates"


def test_a_status_chip_keeps_its_own_class_when_its_level_changes():
    r"""10-10-2026: Configure swapped the chip's level with /status-\w+/, which matched
    "status-chip" first — after every cloud check the chip lost its look and read as a bare link."""
    for path in sorted((WEB / "templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        assert r"replace(/status-\w+/" not in text, f"{path.name} would also replace 'status-chip'"


def test_every_page_that_carries_a_hold_tip_can_show_it():
    """A data-tip is invisible without the script that shows it; the login page has no base."""
    for path in sorted((WEB / "templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if "data-tip=" in text and "<html" in text:
            assert '{% include "_hold_tip.html" %}' in text, f"{path.name} shows no hold-tips"


def test_a_dropdowns_options_are_readable_whatever_the_select_looks_like():
    """10-10-2026: an active filter's see-through tint made Chrome draw the option list pale, with
    the dark theme's light text on it. Options get the theme's solid colours, always."""
    css = (WEB / "static" / "app.css").read_text(encoding="utf-8")
    assert re.search(r"select option, select optgroup \{ background-color: var\(--panel\); "
                     r"color: var\(--text\); \}", css)
    rule = re.search(r"\.col-filter\.is-set \{([^}]*)\}", css).group(1)
    assert "accent-soft" not in rule and "color-mix" in rule        # solid, not see-through


def test_every_table_is_a_data_grid():
    """10-10-2026: the Devices list's table design for the whole Intouch COTA section — header
    band, alternate rows, fitted columns; 12-10-2026: through the whole application, Web FOTA
    included. A table added later without it fails here."""
    for path in sorted((WEB / "templates").glob("*.html")):
        for tag in re.findall(r"<table\b[^>]*>", path.read_text(encoding="utf-8")):
            assert "data-grid" in tag, f"{path.name}: {tag} is not a data-grid"


def test_one_list_filter_and_one_header_sort_for_both_devices_lists():
    """Web FOTA's Devices and Intouch COTA's Devices take the header sort, the Excel-style list
    filter and the pager from one file, and the filter's script lives once, in the base page."""
    grid = (WEB / "templates" / "_grid.html").read_text(encoding="utf-8")
    for name in ("th_sort", "pick", "pager"):
        assert f"macro {name}(" in grid
    for page in ("devices.html", "cota_devices.html"):
        text = (WEB / "templates" / page).read_text(encoding="utf-8")
        assert '{% from "_grid.html" import th_sort, pick, pager with context %}' in text, page
        assert "macro pick(" not in text and "function multi(" not in text, page
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    assert base.count("function multi(box)") == 1


def test_the_model_slicer_is_the_list_filter():
    """Overview and Firmware slice by model with the same list filter the Devices tables use."""
    for page in ("overview.html", "firmware.html"):
        text = (WEB / "templates" / page).read_text(encoding="utf-8")
        assert "grid.pick('model'" in text and 'id="model-form"' in text, page


def test_every_data_grid_column_can_be_dragged_wider_or_narrower():
    """10-10-2026: "variable width — expand and contract the column". One script for every
    .data-grid page; a live redraw that replaces its table sets it up again."""
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    assert "table.data-grid" in base and "col-resize" in base and "window.resizableGrids = all" in base
    job = (WEB / "templates" / "cota_job.html").read_text(encoding="utf-8")
    assert "window.resizableGrids()" in job
    css = (WEB / "static" / "app.css").read_text(encoding="utf-8")
    assert ".data-grid.is-sized { table-layout: fixed; }" in css and ".col-resize {" in css
