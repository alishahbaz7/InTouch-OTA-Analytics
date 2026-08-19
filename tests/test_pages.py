"""Render every page against a real database.

Two production bugs got through a green suite because nothing here ever rendered a template:
a window tuple changed shape and `/changes` raised on every request, and a mid-ingest snapshot
with no rows made the overview blow up. Unit tests cannot catch either — only rendering can.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ota_analytics import config, db, ingest, registry, rollup, scheduler, sources  # noqa: E402


def api_device(imei: str, firmware: str, *, base="7.5.0.27", target=None, model="LOCAT140VB",
               task=None, ping_hours_ago=2.0) -> dict:
    when = datetime.now() - timedelta(hours=ping_hours_ago)
    return {
        "deviceId": imei, "deviceName": imei, "createdBy": 1, "createdByName": "riya",
        "creationTime": 1720009238354,
        "lastPingTime": int(when.timestamp() * 1000),
        "currFirmVer": firmware, "baseFirm": base,
        "updateFirmVer": target or firmware,
        "currConfigVersion": "2.2.2", "model": model, "hwVer": "1.2.0",
        "vin": "MAT562014RKP83714", "iccid": "8991922305932268741F",
        "type_Task": {} if task is None else task,
        "groupNames": "49A 7k, 51A 4K",
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A dashboard backed by a temp database holding two snapshots and a real change."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "scheduler.json")
    monkeypatch.setattr(sources, "SETTINGS_PATH", tmp_path / "connection.json")
    monkeypatch.setattr(scheduler, "_scheduler", None)

    conn = db.connect()
    first = ingest.ingest_records(conn, [
        api_device("111", "7.5.0.27", target="7.5.0.51A", task={"1": 1400}),
        api_device("222", "7.5.0.51A"),
        api_device("333", "2.0.0125", model="TML_Ax1", base="2.0.0125", target="2.0.0162",
                   task={"1": 1400}, ping_hours_ago=900),
        api_device("444", "7.5.0.51A", ping_hours_ago=0.2),
    ], source_name="API test", snapshot_at=datetime.now() - timedelta(hours=6))

    second = ingest.ingest_records(conn, [
        api_device("111", "7.5.0.51A", target="7.5.0.51A"),        # completed an upgrade
        api_device("222", "7.5.0.27", base="7.5.0.27", target="7.5.0.27"),   # fell back to base
        api_device("333", "2.0.0125", model="TML_Ax1", base="2.0.0125", target="2.0.0162",
                   task={"1": 1400}, ping_hours_ago=900),
        api_device("444", "7.5.0.51A", ping_hours_ago=0.2),
    ], source_name="API test", snapshot_at=datetime.now())

    for result in (first, second):
        rollup.rollup_snapshot(conn, result.snapshot_id)

    from ota_analytics import api
    return TestClient(api.app, raise_server_exceptions=False)


PAGES = ["/", "/pending", "/firmware", "/changes", "/devices", "/reachability",
         "/groups", "/quality", "/update", "/errors"]


def test_the_model_picker_is_the_same_control_on_every_page(client):
    """Overview and Firmware both slice by model, so they must not do it two different ways.

    Firmware used an always-open multi-select list box that needed "ctrl+click to pick several"
    written underneath it; the overview had a dropdown. Same job, same control.
    """
    for path in ("/", "/firmware"):
        body = client.get(path).text
        assert 'id="model-picker"' in body, f"{path} has no model dropdown"
        assert 'class="dropdown-list"' in body
        assert 'type="checkbox" name="model"' in body
        assert "ctrl+click" not in body, f"{path} still explains a multi-select"
        assert "<select name=\"model\" multiple" not in body


def test_the_firmware_picker_keeps_the_snapshot_being_viewed(client):
    """Narrowing by model must not silently jump back to the latest snapshot."""
    from ota_analytics import db, metrics

    oldest = metrics.snapshots(db.connect())[-1]["id"]
    body = client.get(f"/firmware?snapshot={oldest}").text
    assert f'name="snapshot" value="{oldest}"' in body


def test_selecting_a_model_on_the_firmware_page_filters_it(client):
    from ota_analytics import db, metrics

    conn = db.connect()
    models = [r["label"] for r in metrics.task_state_by(conn, metrics.latest_snapshot_id(conn),
                                                        "model")]
    assert len(models) > 1, "fixture needs at least two models to prove filtering"

    body = client.get(f"/firmware?model={models[0]}").text
    assert body.count(f'value="{models[0]}"') >= 1

    # The summary reports the narrowing rather than still claiming the whole fleet. Compared
    # with whitespace collapsed, because the template wraps the line and the exact run of
    # spaces is not the thing under test.
    import re
    flat = re.sub(r"\s+", " ", body)
    assert f"in 1 of {len(models)} models" in flat
    assert "all {} models".format(len(models)) not in flat


def test_the_firmware_table_names_each_denominator_and_task_column(client):
    """Three percentage columns sit side by side and measure against different totals.

    One repeated "Share (%)" would put the same word on three different meanings in adjacent
    columns. The group row carries the subject, the row under it carries the denominator — and
    now a Task figure for each, so "how many of these are still waiting" is answerable per
    group rather than only for the row as a whole.
    """
    import re

    body = client.get("/firmware").text
    flat = re.sub(r"\s+", " ", body)

    assert "% of fleet" in flat and "% of version" in flat
    assert flat.count("% of version") == 2          # online and offline, not the fleet share
    assert "Share (%)" not in flat                  # the ambiguous label it replaced

    # Read the group row itself rather than searching the whole page, so a heading appearing
    # somewhere else cannot make this pass.
    header = flat[flat.index('<tr class="group-row">'):flat.index("</thead>")]
    for heading in ("Model", "Firmware", "Devices", "Online", "Offline", "Distribution"):
        assert f">{heading}<" in header, f"{heading} is missing from the header"

    # Three groups, each Count / % / Task.
    assert header.count("Count") == 3
    assert header.count(">Task<") == 3

    # The column of zeros is gone: Inactive read 0 on 102 of 103 rows on the real fleet.
    assert "Inactive" not in header


def test_devices_that_never_pinged_are_still_accounted_for(client):
    """Online + Offline does not reach the row total: STATUS has a third value.

    The Inactive column was dropped because it reads 0 on 102 of 103 rows on the real fleet —
    but those 645 devices must not vanish silently, or one row loses most of itself with nothing
    to explain it. The figure moved into a marker on that row instead of a column of zeros.
    """
    from ota_analytics import db, metrics

    conn = db.connect()
    rows = metrics.firmware_mix(conn, metrics.latest_snapshot_id(conn))
    assert rows, "fixture has no firmware rows"
    for row in rows:
        assert row["online"] + row["offline"] + row["inactive"] == row["devices"], row

    template = Path("ota_analytics/web/templates/firmware.html").read_text(encoding="utf-8")
    assert "never pinged" in template
    assert "footmark" in template


def test_each_group_reports_its_own_pending_count(client):
    """Task pending is split the same way the fleet is, because the split is the point.

    A task pending on a reachable device is the one worth chasing; on a dark one it is parked.
    Reported per version so a rollout stalling on one build stands out from one merely waiting
    for vehicles to be switched on.
    """
    from ota_analytics import db, metrics

    conn = db.connect()
    for row in metrics.firmware_mix(conn, metrics.latest_snapshot_id(conn)):
        assert row["pending_online"] <= row["online"]
        assert row["pending_offline"] <= row["offline"]
        # The two never exceed the row's total pending: an Inactive device can be pending too,
        # so they may sum to less, but never to more.
        assert row["pending_online"] + row["pending_offline"] <= row["pending"]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text[:400]
    assert "Something went wrong" not in response.text


@pytest.mark.parametrize("path,query", [
    ("/", "window=1h"), ("/", "window=yesterday"), ("/", "window=all"),
    ("/", "model=LOCAT140VB"), ("/", "model=LOCAT140VB&model=TML_Ax1"),
    ("/changes", "window=6h"), ("/changes", "window=month"),
    ("/devices", "sort=firmware&dir=asc"), ("/devices", "status=Online&changed=24h"),
    ("/devices", "queue_state=pending&sort=seen&dir=desc"),
    ("/firmware", "model=LOCAT140VB"),
])
def test_pages_render_with_their_filters(client, path, query):
    response = client.get(f"{path}?{query}")
    assert response.status_code == 200, response.text[:400]


def test_overview_shows_the_change_that_happened(client):
    """111 upgraded and 222 fell back to base — both must appear."""
    body = client.get("/?window=all").text
    assert "What changed" in body
    assert "Fell back" in body


def test_changes_page_lists_the_fallback(client):
    body = client.get("/changes?window=all").text
    assert "222" in body                    # the device that returned to base
    assert "7.5.0.27" in body               # its base firmware


def test_json_endpoints_respond(client):
    for path in ("/api/kpis", "/api/version", "/api/agent", "/api/pending", "/api/quality"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.json() is not None


def test_a_snapshot_still_being_written_is_not_served(client, tmp_path):
    """Ingest writes the snapshot row before its devices; that gap broke every page."""
    conn = db.connect()
    conn.execute("INSERT INTO snapshot (source_file, file_sha256, snapshot_at, ts_source, "
                 "row_count, ingested_at) VALUES ('half done','sha-x',?,'api',0,?)",
                 (datetime.now().isoformat(sep=" ", timespec="seconds"),
                  datetime.now().isoformat(sep=" ", timespec="seconds")))
    conn.commit()

    response = client.get("/")
    assert response.status_code == 200
    assert "half done" not in response.text


def test_an_unknown_window_falls_back_instead_of_failing(client):
    assert client.get("/changes?window=nonsense").status_code == 200
    assert client.get("/?window=nonsense").status_code == 200


def test_an_unknown_sort_column_is_ignored(client):
    assert client.get("/devices?sort=DROP+TABLE&dir=sideways").status_code == 200


def test_version_is_reported_everywhere(client):
    from ota_analytics import __version__

    assert client.get("/api/version").json()["version"] == __version__
    assert f"v{__version__}" in client.get("/").text


# ─── one status chip, one theme switch, one table idiom ─────────────────────

def test_the_header_carries_one_freshness_chip_not_two(client):
    """"Updated 11:08 · 8 min ago" and "Next 4:02" answer the same question.

    Side by side they read as two unrelated clocks; together they say when the data was true and
    when it will next be checked.
    """
    import re

    flat = re.sub(r"\s+", " ", client.get("/").text)
    assert flat.count('class="status-chip') == 2      # connection state, and this one
    assert flat.count('id="agent-chip"') == 1
    assert "Updated" in flat
    # The countdown writes into the same chip rather than a second one.
    assert 'id="agent-text"' in flat
    chip = flat[flat.index('id="agent-chip"'):]
    chip = chip[:chip.index("</a>")]
    assert "Updated" in chip and 'id="agent-text"' in chip


def test_a_theme_can_be_chosen_or_left_to_the_system(client):
    """Three states, and the CSS ordering is what makes all three reachable.

    A light palette defined only inside a prefers-color-scheme query could never be turned on by
    someone whose system is dark, so each explicit choice is stated on its own.
    """
    from pathlib import Path

    body = client.get("/").text
    assert 'id="theme-btn"' in body
    # Applied before the stylesheet paints, or a reader who chose light gets a flash of dark.
    assert body.index("ota-theme") < body.index("</head>")

    css = Path("ota_analytics/web/static/app.css").read_text(encoding="utf-8")
    assert ':root[data-theme="light"]' in css
    assert ':root[data-theme="dark"]' in css
    # The system preference must not override an explicit choice.
    assert ':root:not([data-theme="dark"])' in css


def test_both_grouped_tables_share_the_same_idiom(client):
    """Firmware and the model table are read the same way, without being identical.

    The model table carries more — it has room, at six rows against a hundred — so this checks
    the idiom rather than the column count: a two-tier header, every percentage named by its
    denominator, and a totals row pinned with the header instead of left at the foot.
    """
    import re

    def header_of(path, table_id):
        flat = re.sub(r"\s+", " ", client.get(path).text)
        assert f'id="{table_id}"' in flat, f"{path} has no {table_id} table"
        block = flat[flat.index(f'id="{table_id}"'):]
        return block[:block.index("</thead>")]

    firmware = header_of("/firmware", "firmware-mix")
    models = header_of("/", "model-states")

    for header in (firmware, models):
        assert 'class="group-row"' in header and 'class="sub-row"' in header
        assert 'class="totals-row"' in header
        for group in ("Devices", "Online", "Offline"):
            assert f">{group}<" in header
        # A bare "Share" repeated across columns with different denominators is what this
        # replaced; every percentage says what it is a percentage of.
        assert "Share (%)" not in header
        assert "% of" in header

    assert "% of fleet" in firmware and "% of version" in firmware
    assert "% of fleet" in models and "% of model" in models


def test_the_model_table_adds_up_two_ways(client):
    """Two accountings of the same devices, and each has to reach the row total.

        Completed + Pending + No task  = Devices
        Online + Offline + Act-Pending = Devices

    The second is why Activation-Pending is a column here and a marker on the firmware table:
    without it the (unknown) row reads 0 online and 36 offline out of 513 and loses 477 devices
    with nothing to explain them.
    """
    from ota_analytics import db, metrics

    conn = db.connect()
    rows = metrics.task_state_by(conn, metrics.latest_snapshot_id(conn), "model")
    assert rows, "fixture has no models"

    for row in rows:
        assert row["completed"] + row["pending"] + row["never_tasked"] == row["devices"], row
        assert row["online"] + row["offline"] + row["inactive"] == row["devices"], row

    header = client.get("/").text
    for heading in ("Completed", "No task", "Activation-", "Pending"):
        assert heading in header


def test_the_model_table_colours_what_is_worth_acting_on(client):
    """Pending-while-online is the number to chase; the totals are not coloured at all.

    The yellow one is conditional on being non-zero, so it is asserted against the template —
    this fixture has no reachable pending device to render it.
    """
    from pathlib import Path as _Path

    template = _Path("ota_analytics/web/templates/overview.html").read_text(encoding="utf-8")
    assert "tone-yellow-text" in template and "if r.pending_reachable" in template
    assert "tone-orange-text" in template

    # The Devices group has no Task column: that figure is Pending, and printing it twice under
    # two headings invites the reader to look for a difference that cannot exist.
    header = re.sub(r"\s+", " ", client.get("/").text)
    block = header[header.index('id="model-states"'):]
    block = block[:block.index("</thead>")]
    assert block.count(">Task<") == 2          # Online and Offline only


def test_each_group_in_the_model_table_reports_its_own_pending(client):
    from ota_analytics import db, metrics

    conn = db.connect()
    for row in metrics.task_state_by(conn, metrics.latest_snapshot_id(conn), "model"):
        assert row["pending_reachable"] <= row["online"]
        assert row["pending_offline"] <= row["offline"]
        # They may sum to less than the row's total pending — a device that has never pinged can
        # carry a task too — but never to more.
        assert row["pending_reachable"] + row["pending_offline"] <= row["pending"]


# ─── the devices filter row ─────────────────────────────────────────────────

def test_firmware_is_a_checkbox_list_of_versions_only(client):
    """Several versions are usually interesting together — the ones a rollout moves between.

    A single-choice select meant one page load per version. The device counts are gone from the
    list because they were the widest thing in it and are already on the Firmware page.
    """
    flat = re.sub(r"\s+", " ", client.get("/devices").text)
    assert 'id="firmware-picker"' in flat
    assert 'id="firmware-all"' in flat            # a real toggle, not a reset link
    assert 'type="checkbox" name="firmware"' in flat
    assert '<select name="firmware"' not in flat

    picker = flat[flat.index('id="firmware-picker"'):]
    picker = picker[:picker.index("</details>")]
    versions = re.findall(r'name="firmware" value="([^"]+)"', picker)
    assert versions, "no versions offered"
    # Only the All row carries a count; the versions themselves are bare.
    listing = picker[picker.index('class="dropdown-list"'):]
    assert "<em>" not in listing


def test_several_firmware_versions_can_be_selected_at_once(client):
    from ota_analytics import db, metrics

    conn = db.connect()
    sid = metrics.latest_snapshot_id(conn)
    versions = [r["firmware"] for r in metrics.firmware_mix(conn, sid) if r["firmware"]][:2]
    assert len(versions) == 2, "fixture needs two firmware versions"

    one = client.get(f"/devices?firmware={versions[0]}").text
    both = client.get(f"/devices?firmware={versions[0]}&firmware={versions[1]}").text

    def total(body):
        return int(re.search(r'class="hint">([\d,]+) ', re.sub(r"\s+", " ", body))
                   .group(1).replace(",", ""))

    assert total(both) > total(one), "adding a version did not widen the selection"
    # Both stay ticked, so the control shows what it is filtering by.
    flat = re.sub(r"\s+", " ", both).replace('checked=""', "checked")
    for version in versions:
        assert f'value="{version}" checked' in flat


def test_the_group_text_box_is_gone_but_the_link_still_works(client):
    """It was an exact-match field nobody could type from memory. The Groups page links here
    with ?group=…, so the capability stays even though the control does not."""
    flat = re.sub(r"\s+", " ", client.get("/devices").text)
    assert 'placeholder="exact name"' not in flat

    from ota_analytics import db, metrics
    conn = db.connect()
    groups = metrics.groups(conn, metrics.latest_snapshot_id(conn))
    if groups:
        name = groups[0]["group_name"]
        filtered = client.get(f"/devices?group={name}")
        assert filtered.status_code == 200
        # And the filter survives paging, so it is carried in a hidden field.
        assert f'name="group" value="{name}"' in re.sub(r"\s+", " ", filtered.text)


def test_an_imei_can_be_searched_by_any_part_of_it(client):
    """The last few digits are what someone reads off a label, and a paste from the platform
    arrives wrapped in quotes and commas."""
    body = client.get("/devices").text
    imeis = re.findall(r'class="mono">(\d+)<', body)
    assert imeis, "fixture has no devices"
    target = imeis[0]

    def found(term):
        import urllib.parse
        page = client.get("/devices?q=" + urllib.parse.quote(term)).text
        return re.findall(r'class="mono">(\d+)<', page)

    assert found(target) == [target]
    assert target in found(target[-2:])
    # Non-digits are stripped rather than rejected, so a paste works as-is.
    assert found(f'"{target}", ') == [target]
    # And nothing matches an IMEI that is not there.
    assert found("00000000000000") == []
