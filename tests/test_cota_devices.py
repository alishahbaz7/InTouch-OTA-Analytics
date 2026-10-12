"""The Intouch COTA Devices page (the user's design, 10-10-2026): every device by its cloud id,
filtered and sorted on any column; added by id or file with tags; one group per device, moved
between groups with a say over the ones already elsewhere; the last job kept past the day."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import unquote

from ota_analytics import cota, cota_campaign, cota_fleet, db
from tests.test_pages import client, cloud, cota_env, no_job_threads  # noqa: F401  (fixtures)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _ids(body: str) -> list[int]:
    """The device ids the page lists, in order."""
    return [int(x) for x in re.findall(r'data-row-sel value="(\d+)"', body)]


def _fota(conn, imei: str, *, model: str, firmware: str, seen: datetime) -> None:
    stamp = seen.strftime(cota.TIME_FORMAT)
    with conn:
        conn.execute("INSERT OR REPLACE INTO device (imei, device_model, firmware, status, seen_at, "
                     "first_seen_at, last_checked_at) VALUES (?, ?, ?, 'Online', ?, ?, ?)",
                     (imei, model, firmware, stamp, stamp, stamp))


def _put(conn, *ids: int) -> None:
    with conn:
        for d in ids:
            cota.put_device(conn, d)


# ── adding devices ───────────────────────────────────────────────────────────────────────────

def test_add_device_takes_ids_and_tags_and_a_second_add_only_adds_tags(client):
    reply = client.post("/cota/devices/add", data={"devices": "14906, 786\n786", "tags": "Delhi, pilot"},
                        follow_redirects=False)
    assert reply.status_code == 303 and "note=added:2:0:0" in unquote(reply.headers["location"])
    page = _flat(client.get(reply.headers["location"]).text)
    assert "2 devices added." in page and sorted(_ids(page)) == [786, 14906]
    client.post("/cota/devices/add", data={"devices": "786", "tags": "north"})
    conn = db.connect()
    assert conn.execute("SELECT tags FROM cota_device WHERE device_id = 786").fetchone()[0] == "Delhi, pilot, north"
    assert conn.execute("SELECT COUNT(*) FROM cota_device").fetchone()[0] == 2


def test_add_device_refuses_an_imei_and_reopens_with_what_was_typed(client):
    body = client.post("/cota/devices/add", data={"devices": "865510083360422", "tags": "x"}).text
    assert "looks like an IMEI" in body and "865510083360422</textarea>" in body
    assert re.search(r'id="add-dialog"[^>]*data-autoopen', body)
    assert db.connect().execute("SELECT COUNT(*) FROM cota_device").fetchone()[0] == 0


def test_add_device_reads_the_clouds_list_with_imei_and_tags(client):
    csv = 'id,trackingCode,tags\n14906,865510083360422,"Delhi, pilot"\n786,,Delhi\nabc,,\n'
    reply = client.post("/cota/devices/add", files={"file": ("d.csv", csv.encode(), "text/csv")},
                        follow_redirects=False)
    page = _flat(client.get(reply.headers["location"]).text)
    assert "2 devices added · 1 line left out with a problem." in page
    conn = db.connect()
    assert cota.imei_for(conn, 14906) == "865510083360422"
    assert cota_fleet.tag_devices(conn, "delhi") == [786, 14906]
    bare = client.post("/cota/devices/add", files={"file": ("d.csv", b"14906\n786\n", "text/csv")},
                       follow_redirects=False)
    assert "added:0:2:0" in unquote(bare.headers["location"])         # no header needed
    template = client.get("/cota/devices/template.csv").content.decode("utf-8-sig")
    assert template.splitlines()[0] == "id,trackingCode,tags"


# ── the list: every column filters and sorts ─────────────────────────────────────────────────

def test_every_column_filters_and_sorts(client, cloud, no_job_threads):
    conn = db.connect()
    now = datetime.now()
    with conn:
        cota.put_device(conn, 1, imei="111111111111111")
        cota.put_device(conn, 2, imei="222222222222222")
        cota.put_device(conn, 3)
    _fota(conn, "111111111111111", model="AIS140", firmware="7.5.1A", seen=now - timedelta(minutes=10))
    _fota(conn, "222222222222222", model="LOCAT", firmware="7.4.27C", seen=now - timedelta(days=3))
    cota_fleet.add_tags(conn, [1, 3], "Delhi")
    north = cota_fleet.create_group(conn, "North", [1, 2])["group_id"]

    get = lambda q="": _ids(client.get("/cota/devices?" + q).text)
    assert get() == [1, 2, 3]
    assert get("id=2") == [2] and get("imei=1111") == [1]
    assert get("model=AIS140") == [1] and get("model=(none)") == [3]
    assert get(f"group={north}") == [1, 2] and get("group=none") == [3]
    assert get("fw=7.4.27C") == [2] and get("fw=(none)") == [3]
    assert get("seen=hour") == [1] and get("seen=week") == [2] and get("seen=none") == [3]
    assert get("tag=delhi") == [1, 3] and get("tag=(none)") == [2]
    assert get("sort=id&dir=desc") == [3, 2, 1]
    assert get("sort=seen&dir=desc") == [1, 2, 3]                     # no ping last, either way
    assert get("sort=model") == [1, 2, 3] and get("sort=fw") == [2, 1, 3]

    client.post("/cota/jobs/start", data={"name": "FTP check", "device_ids": "2", "commands": "DAD76F4B"})
    assert get("job=FTP check") == [2] and get("status=In progress") == [2]
    assert get("status=(none)") == [1, 3]
    row = _flat(client.get("/cota/devices?id=2").text)
    assert 'class="pill pill-js-in-progress">In progress</span>' in row and "FTP check" in row

    page = client.get(f"/cota/devices?group={north}&sort=seen&dir=desc").text
    assert 'aria-sort="descending"' in page and '<span class="multi-text">North</span>' in page
    assert re.search(rf'<input type="checkbox" value="{north}" checked>', page)
    assert re.search(r'<input type="checkbox" value="none" >', page)               # not ticked
    assert "Clear filters" in page and "2 of 3" in _flat(page)


def test_a_list_filter_takes_any_number_of_values(client):
    """Excel's filter (10-10-2026): all, some, inverted, one — several values of one column are
    OR'd; columns are AND'd; the empty case is a value too."""
    conn = db.connect()
    now = datetime.now()
    with conn:
        for d in (1, 2, 3, 4):
            cota.put_device(conn, d, imei=f"{d}" * 15 if d < 4 else None)
    _fota(conn, "111111111111111", model="AIS140", firmware="7.5.1A", seen=now)
    _fota(conn, "222222222222222", model="LOCAT", firmware="7.4.27C", seen=now - timedelta(days=3))
    _fota(conn, "333333333333333", model="LOCAT", firmware="7.4.28", seen=now - timedelta(days=30))
    cota_fleet.add_tags(conn, [1], "Delhi")
    cota_fleet.add_tags(conn, [2], "pilot")
    get = lambda q="": _ids(client.get("/cota/devices?" + q).text)
    assert get("fw=7.5.1A&fw=7.4.28") == [1, 3]
    assert get("fw=(none)&fw=7.4.27C") == [2, 4]
    assert get("seen=hour&seen=older") == [1, 3]
    assert get("tag=Delhi&tag=pilot") == [1, 2] and get("tag=pilot&tag=(none)") == [2, 3, 4]
    assert get("model=LOCAT&fw=7.4.28") == [3]                         # columns AND
    assert get("group=none&id=2") == [2]
    assert get("id=1, 3") == [1, 3] and get("id=1") == [1]              # a pasted list: exactly these
    page = client.get("/cota/devices?fw=7.5.1A&fw=7.4.28").text
    assert '<span class="multi-text">2 selected</span>' in page
    assert page.count('name="fw" value="7.') == 2                      # kept for the next filter
    assert 'href="/cota/devices/export?fw=7.5.1A&amp;fw=7.4.28' in page     # the view, exported
    counts = dict((v, n) for v, _, n in cota_fleet.options(conn)["choices"]["model"])
    assert counts == {"(none)": 1, "AIS140": 1, "LOCAT": 2}
    moved = client.post("/cota/devices/action", data={"action": "tag", "scope": "all", "tag": "x",
                                                      "query": "fw=7.5.1A&fw=7.4.28", "back": "/cota/devices"})
    assert cota_fleet.tag_devices(conn, "x") == [1, 3]                  # "all matching" honours them


def test_a_view_is_its_url_and_pages_keep_it(client):
    conn = db.connect()
    _put(conn, *range(1, 61))
    cota_fleet.add_tags(conn, list(range(1, 61)), "Delhi")
    page = client.get("/cota/devices?tag=Delhi&size=25&page=2").text
    assert _ids(page) == list(range(26, 51))
    assert 'href="/cota/devices?tag=Delhi&amp;sort=id&amp;dir=asc&amp;size=25&amp;page=3"' in page
    assert "26–50 of <strong>60</strong>" in page


# ── groups: overlap is a warning, never a restriction (10-10-2026) ──────────────────────────

def test_a_new_group_warns_about_devices_elsewhere_but_takes_them_too(client):
    conn = db.connect()
    a = cota_fleet.create_group(conn, "A", [1, 2, 3])["group_id"]
    check = client.post("/cota/groups/check", data={"source": "ids", "devices": "2, 3, 4"}).json()
    assert (check["total"], check["new"], check["free"], check["elsewhere_total"]) == (3, 1, 1, 2)
    assert check["elsewhere"][0]["name"] == "A"
    assert [d["device_id"] for d in check["elsewhere"][0]["devices"]] == [2, 3]

    # Everything joins unless left out; those already in A stay in A as well.
    reply = client.post("/cota/groups", data={"name": "B", "source": "ids", "devices": "2, 3, 4",
                                              "skip": "2"}, follow_redirects=False)
    b = int(re.search(r"group=(\d+)", reply.headers["location"]).group(1))
    assert "Group created with 2 devices · 1 left out · 1 also in another group." in _flat(
        client.get(reply.headers["location"]).text)
    assert cota_campaign.group_devices(conn, a) == [1, 2, 3] and cota_campaign.group_devices(conn, b) == [3, 4]
    # "Take them out of their other groups" makes it a move.
    moved = client.post("/cota/groups", data={"name": "C", "source": "ids", "devices": "1, 3",
                                              "transfer": "true"}, follow_redirects=False)
    assert "1 moved from another group" not in unquote(moved.headers["location"])
    c = int(re.search(r"group=(\d+)", moved.headers["location"]).group(1))
    assert cota_campaign.group_devices(conn, c) == [1, 3]
    assert cota_campaign.group_devices(conn, a) == [2] and cota_campaign.group_devices(conn, b) == [4]

    body = client.post("/cota/groups", data={"name": "b", "source": "ids", "devices": "9"}).text
    assert "A group is already called “b”" in body and re.search(r'id="group-dialog"[^>]*data-autoopen', body)
    bad = client.post("/cota/groups", data={"name": "D", "source": "ids", "devices": "14906, 78x6"}).text
    assert "78x6" in bad and conn.execute("SELECT COUNT(*) FROM cota_group").fetchone()[0] == 3


def test_the_group_dialog_lists_devices_elsewhere_as_a_table_and_warns(client):
    body = client.get("/cota/devices").text
    assert 'id="check-warn"' in body and 'id="group-transfer"' in body
    assert re.search(r'<table class="data-grid check-table">\s*<thead><tr><th class="fit"><input type="checkbox" id="check-all"', body)
    assert 'data-pick="invert"' in body and 'name="skip" id="group-skip"' in body


def test_a_group_can_be_made_from_a_tag_or_the_selection(client):
    conn = db.connect()
    _put(conn, 1, 2, 3)
    cota_fleet.add_tags(conn, [2, 3], "pilot")
    by_tag = client.post("/cota/groups/check", data={"source": "tag", "tag": "Pilot"}).json()
    assert by_tag["total"] == 2 and by_tag["free"] == 2
    client.post("/cota/groups", data={"name": "Pilot", "source": "tag", "tag": "pilot"})
    picked = client.post("/cota/groups", data={"name": "Rest", "source": "selection", "scope": "all",
                                               "query": "group=none"}, follow_redirects=False)
    rest = int(re.search(r"group=(\d+)", picked.headers["location"]).group(1))
    assert cota_campaign.group_devices(conn, rest) == [1]


def test_add_to_group_keeps_other_groups_unless_told_to_move(client):
    conn = db.connect()
    a = cota_fleet.create_group(conn, "A", [1, 2])["group_id"]
    b = cota_fleet.create_group(conn, "B", [3])["group_id"]
    check = client.post("/cota/groups/check", data={"source": "selection", "ids": "1,3",
                                                    "group_id": str(b)}).json()
    assert check["here"] == 1 and check["elsewhere_total"] == 1
    act = lambda **form: client.post("/cota/devices/action", data={"back": f"/cota/devices?group={b}", **form},
                                     follow_redirects=False)
    reply = act(action="add", ids="1", group_id=str(b))
    assert unquote(reply.headers["location"]) == f"/cota/devices?group={b}&note=joined:1:1:0"
    assert "1 device in the group · 1 also in another group." in _flat(client.get(reply.headers["location"]).text)
    assert cota_campaign.group_devices(conn, a) == [1, 2] and cota_campaign.group_devices(conn, b) == [3, 1]
    act(action="add", ids="2", group_id=str(b), transfer="true")                    # a move
    assert cota_campaign.group_devices(conn, a) == [1] and 2 in cota_campaign.group_devices(conn, b)
    # Take out of group, from the group in view: the device keeps any other group.
    act(action="ungroup", ids="1", from_group=str(b))
    assert cota_campaign.group_devices(conn, a) == [1] and 1 not in cota_campaign.group_devices(conn, b)
    renamed = client.post(f"/cota/groups/{b}/rename", data={"name": "Bee"}, follow_redirects=False)
    assert "Group renamed." in client.get(renamed.headers["location"]).text
    gone = client.post(f"/cota/groups/{a}/delete", follow_redirects=False)
    assert gone.headers["location"] == "/cota/devices?note=groupdeleted"
    assert conn.execute("SELECT COUNT(*) FROM cota_device WHERE device_id = 1").fetchone()[0] == 1


def test_edit_group_renames_and_adds_devices_in_bulk(client):
    """10-10-2026: 1,000+ devices to put in a group — too many to tick on the page. Edit takes a
    pasted list (or a tag, or the ticked rows) and a new name, either or both."""
    conn = db.connect()
    gj = cota_fleet.create_group(conn, "Gujarati", [1, 2])["group_id"]
    other = cota_fleet.create_group(conn, "device list", list(range(100, 1300)))["group_id"]
    page = client.get(f"/cota/devices?group={gj}").text
    assert re.search(rf'data-edit-group data-id="{gj}" data-name="Gujarati"', page)

    renamed = client.post(f"/cota/groups/{gj}/edit", data={"name": "Gujarat", "source": "ids", "devices": ""},
                          follow_redirects=False)
    assert "Group renamed." in _flat(client.get(renamed.headers["location"]).text)
    assert conn.execute("SELECT name FROM cota_group WHERE id = ?", (gj,)).fetchone()[0] == "Gujarat"

    pasted = "\n".join(str(d) for d in range(100, 1300))                # a column from Excel
    check = client.post("/cota/groups/check", data={"source": "ids", "devices": pasted + "\n1",
                                                    "group_id": str(gj)}).json()
    assert (check["total"], check["here"], check["elsewhere_total"]) == (1201, 1, 1200)
    moved = client.post(f"/cota/groups/{gj}/edit", data={"name": "Gujarat", "source": "ids", "devices": pasted,
                                                         "transfer": "true", "skip": "1299"},
                        follow_redirects=False)
    assert "Group saved · 1,199 devices added · 1,199 moved from another group." in _flat(
        client.get(moved.headers["location"]).text)
    assert len(cota_campaign.group_devices(conn, gj)) == 2 + 1199
    assert cota_campaign.group_devices(conn, other) == [1299]                # the one left out stays

    clash = client.post(f"/cota/groups/{gj}/edit", data={"name": "device list", "source": "ids", "devices": ""}).text
    assert "A group is already called “device list”" in clash
    assert re.search(rf'id="group-dialog"[^>]*data-autoopen\s+data-edit-id="{gj}"', clash)
    assert client.post("/cota/groups/9999/edit", data={"name": "x"}, follow_redirects=False).status_code == 303


def test_a_device_in_several_groups_shows_each_without_a_warning(client):
    conn = db.connect()
    _put(conn, 5, 6)
    one = cota_fleet.create_group(conn, "Old 1", [5, 6])["group_id"]
    cota_fleet.create_group(conn, "Old 2", [5])
    page = _flat(client.get("/cota/devices").text)
    assert "in more than one group" not in page                       # allowed: no notice
    assert '<span class="group-tag">Old 1</span><span class="group-tag">Old 2</span>' in page
    assert _ids(client.get("/cota/devices?group=several").text) == [5]       # an old link still works
    assert cota_campaign.group_devices(conn, one) == [5, 6]
    # The Group filter: each group by its own name, nothing merged (the user, 10-10-2026). A device
    # in two groups matches either.
    two = conn.execute("SELECT id FROM cota_group WHERE name = 'Old 2'").fetchone()[0]
    labels = [label for _, label, _ in cota_fleet.options(conn)["choices"]["group"]]
    assert labels == ["No group", "Old 1", "Old 2"] and not any("+" in x or "several" in x for x in labels)
    assert _ids(client.get(f"/cota/devices?group={two}").text) == [5]
    assert _ids(client.get(f"/cota/devices?group={one}").text) == [5, 6]


def test_a_filter_lists_only_values_some_device_has(client):
    """10-10-2026: "No group 0" offered a value that matches nothing. A value with no devices is
    left out — unless it is ticked, so it can be unticked."""
    conn = db.connect()
    _put(conn, 1, 2)
    cota_fleet.create_group(conn, "Everyone", [1, 2])
    with conn:
        conn.execute("INSERT INTO cota_group (name, created_at) VALUES ('Empty', 'x')")
    panel = lambda q="": client.get("/cota/devices?" + q).text.split('data-multi="group"')[1].split("</div>\n</div>")[0]
    assert "No group" not in panel("") and "Empty" not in panel("") and "Everyone" in panel("")
    assert re.search(r'value="none" checked>\s*<span class="multi-label" title="No group">No group</span><span class="multi-count">0', panel("group=none"))


# ── what the selection bar does ──────────────────────────────────────────────────────────────

def test_the_selection_bar_tags_starts_a_job_exports_and_removes(client):
    conn = db.connect()
    _put(conn, 1, 2, 3)
    act = lambda **form: client.post("/cota/devices/action", data={"back": "/cota/devices", **form},
                                     follow_redirects=False)
    assert "note=tagged:3" in unquote(act(action="tag", scope="all", query="", tag="Delhi").headers["location"])
    act(action="untag", ids="1", tag="delhi")
    assert cota_fleet.tag_devices(conn, "Delhi") == [2, 3]
    job = act(action="newjob", ids="1,3").text
    assert re.search(r'name="devices"[^>]*>1, 3</textarea>', job)
    export = act(action="export", ids="2")
    lines = export.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("Device ID,IMEI,Device type,Group,Last job,Last job status")
    assert len(lines) == 2 and lines[1].startswith("2,")
    assert "note=deleted:1" in unquote(act(action="delete", ids="3").headers["location"])
    assert conn.execute("SELECT COUNT(*) FROM cota_device").fetchone()[0] == 2
    assert act(action="tag", ids="", tag="x").headers["location"].endswith("note=none")


def test_destructive_actions_ask_first_and_the_way_back_stays_on_the_page(client):
    _put(db.connect(), 1)
    body = client.get("/cota/devices").text
    assert re.search(r'data-action="delete" data-confirm-action="Remove"\s+data-confirm="Remove the selected', body)
    assert 'class="tool-btn danger" data-action="delete"' in body
    away = client.post("/cota/devices/action", data={"action": "tag", "ids": "1", "tag": "x",
                                                     "back": "https://example.com/cota/devices"},
                       follow_redirects=False)
    assert away.headers["location"].startswith("/cota/devices?")


def test_the_whole_view_downloads_as_excel_or_csv(client):
    conn = db.connect()
    _put(conn, 1, 2)
    cota_fleet.add_tags(conn, [2], "Delhi")
    csv_reply = client.get("/cota/devices/export?tag=Delhi&format=csv")
    assert csv_reply.content.decode("utf-8-sig").splitlines()[1].startswith("2,")
    xlsx = client.get("/cota/devices/export?format=xlsx")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"


# ── the last job outlives the day's record ───────────────────────────────────────────────────

def test_a_devices_last_job_is_kept_when_the_day_is_cleared(client, cloud, no_job_threads):
    client.post("/cota/jobs/start", data={"name": "FTP check", "device_ids": "2, 3", "commands": "DAD76F4B"})
    conn = db.connect()
    with conn:
        conn.execute("UPDATE cota_campaign_device SET state = 'done' WHERE device_id = 2")
        conn.execute("UPDATE cota_campaign_result SET state = 'done', attempts = 1 WHERE device_id = 2")
        conn.execute("UPDATE cota_campaign SET created_at = '2026-01-01 10:00:00'")
        assert cota_campaign.keep_last_jobs(conn, "2026-01-02 00:00:00") == 2
        conn.execute("DELETE FROM cota_campaign")
    kept = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT device_id, last_job_name, last_job_status FROM cota_device")}
    assert kept == {2: ("FTP check", "Completed"), 3: ("FTP check", "Not finished")}
    page = _flat(client.get("/cota/devices").text)
    assert 'pill-js-completed">Completed' in page and 'pill-js-not-finished">Not finished' in page


# ── v21: the list is keyed by the cloud's device id ──────────────────────────────────────────

def test_v21_rekeys_the_device_list_by_device_id(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE cota_device (imei TEXT PRIMARY KEY, device_id INTEGER NOT NULL,
            device_type INTEGER NOT NULL, source TEXT, updated_at TEXT NOT NULL) WITHOUT ROWID;
        INSERT INTO cota_device VALUES ('865510083360422', 14906, 124, 'device list', '2026-10-01 10:00:00');
        INSERT INTO cota_device VALUES ('865510083360430', 14906, 124, 'device list', '2026-10-02 10:00:00');
        CREATE TABLE cota_group (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE cota_group_member (group_id INTEGER NOT NULL, device_id INTEGER NOT NULL,
            seq INTEGER NOT NULL, PRIMARY KEY (group_id, device_id)) WITHOUT ROWID;
        INSERT INTO cota_group VALUES (1, 'Desk', '2026-10-01 10:00:00');
        INSERT INTO cota_group_member VALUES (1, 786, 0);
        PRAGMA user_version = 20;
    """)
    old.close()
    conn = db.connect(path)
    rows = [tuple(r) for r in conn.execute("SELECT device_id, imei, source FROM cota_device ORDER BY device_id")]
    assert rows == [(786, None, "group"), (14906, "865510083360430", "device list")]
    with conn:
        assert cota.put_device(conn, 15000) is True                   # an id alone can be listed now
