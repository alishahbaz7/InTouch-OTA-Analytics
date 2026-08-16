"""Smoke tests for the web app itself.

Nothing else in the suite imports `api`, so a module-level error there — a name used before it
is defined, a bad decorator, a template that does not exist — passed every test and still broke
the server on startup. These tests close that gap.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from ota_analytics import api  # noqa: E402


def test_the_app_imports_and_has_its_routes():
    paths = {route.path for route in api.app.routes}
    for expected in ("/", "/pending", "/firmware", "/changes", "/reachability",
                     "/groups", "/devices", "/quality", "/update"):
        assert expected in paths


def test_every_page_template_exists():
    for name in ("overview.html", "pending.html", "firmware.html", "changes.html",
                 "reachability.html", "groups.html", "devices.html", "quality.html",
                 "update.html", "empty.html", "base.html", "macros.html"):
        assert (api.WEB / "templates" / name).exists(), name


def test_sortable_columns_are_whitelisted():
    """A sort parameter must never reach ORDER BY as raw SQL."""
    assert api.DEFAULT_SORT in api.SORTABLE
    assert "seen" in api.SORTABLE

    injected = api._order_by("'; DROP TABLE device; --", "desc")
    assert "DROP" not in injected
    assert injected.startswith(api.SORTABLE[api.DEFAULT_SORT])


@pytest.mark.parametrize("direction,expected", [("asc", "ASC"), ("desc", "DESC"), ("x", "DESC")])
def test_sort_direction_is_constrained(direction, expected):
    assert expected in api._order_by("firmware", direction)


def test_missing_values_sort_last():
    """A device that never reported should not head the list on a NULL timestamp."""
    assert api._order_by("seen", "desc").startswith("d.seen_at IS NULL")


def test_relative_age_reads_naturally():
    from datetime import datetime, timedelta

    now = datetime.now()
    assert api._relative_age((now - timedelta(seconds=30)).isoformat(sep=" ")) == "just now"
    assert api._relative_age((now - timedelta(minutes=12)).isoformat(sep=" ")) == "12 min ago"
    assert api._relative_age((now - timedelta(hours=5)).isoformat(sep=" ")) == "5 hr ago"
    assert api._relative_age((now - timedelta(days=4)).isoformat(sep=" ")) == "4 days ago"
    assert api._relative_age(None) == ""
    assert api._relative_age("not a date") == ""
