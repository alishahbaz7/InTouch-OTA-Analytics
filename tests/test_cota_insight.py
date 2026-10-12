"""The Intouch COTA Overview's figures, from a simulated day — and Web FOTA's view of the devices
that never answered. The fleet and clock are test_cota_campaign's."""

from __future__ import annotations

from datetime import timedelta

from ota_analytics import cota_insight
from tests.test_cota_campaign import GET_6C0A, GET_FTP, fleet  # noqa: F401  (fixture)


def _registry(conn, device, seen_at, firmware="7.5.1A"):
    """A Web FOTA registry row for a simulated device — the fleet names IMEIs this way."""
    imei = f"86551008{device:07d}"
    with conn:
        conn.execute("INSERT INTO device (imei, device_model, firmware, status, seen_at, first_seen_at, "
                     "last_checked_at) VALUES (?, 'LOCAT140VB', ?, 'Online', ?, ?, ?)",
                     (imei, firmware, seen_at, seen_at, seen_at))


def test_nothing_sent_today_is_no_overview(fleet):
    assert cota_insight.overview(fleet.conn) is None


def test_a_day_of_jobs_in_figures(fleet):
    # 101, 102, 105 answer everything — GET 6C0A slowly; 106 answers with a failure; 103 and 104
    # are silent all day: 103 pinged the OTA platform ten minutes ago, 104 three days ago.
    def how(d, v, n):
        if d in (103, 104):
            return "pending"
        if d == 106:
            return "fail:20"
        return "answer:45" if v == GET_6C0A else "answer:20"
    job, _ = fleet([101, 102, 103, 104, 105, 106], text=f"{GET_FTP}\n{GET_6C0A}", how=how)
    now = fleet.clock.wall()
    stamp = lambda delta: (now - delta).strftime("%Y-%m-%d %H:%M:%S")
    for d in (101, 102, 105, 106):
        _registry(fleet.conn, d, stamp(timedelta(minutes=5)))
    _registry(fleet.conn, 103, stamp(timedelta(minutes=10)), firmware="7.4.27C")
    _registry(fleet.conn, 104, stamp(timedelta(days=3)), firmware="7.4.27C")

    o = cota_insight.overview(fleet.conn, now=now)
    devices = {s["label"]: s["value"] for s in o["devices_donut"]}
    assert devices == {"Answered everything": 3, "Answered some": 1, "Answered nothing": 2,
                       "Still in progress": 0}
    reasons = {r["key"]: r["value"] for r in o["reasons"]["rows"]}
    assert reasons == {"not_delivered": 4, "answered_failure": 2}
    assert o["reasons"]["resends_to_silent"] == 8              # 2 devices × 2 commands × 2 more tries

    silent = o["silent"]
    assert silent["total"] == 2
    assert {b["key"]: b["value"] for b in silent["buckets"]} == {"hour": 1, "day": 0, "old": 1}
    assert [d["device_id"] for d in silent["online"]] == [103] and silent["online"][0]["seen_minutes"] == 10
    quiet = [f for f in silent["firmware"] if f["firmware"] == "7.4.27C"][0]
    assert (quiet["devices"], quiet["none"]) == (2, 2)

    slow = {c["name"]: c for c in o["commands"]}
    six = slow["GET 6C0A"]
    assert six["median"] > six["wait"] == 30 and six["suggest_wait"] == cota_insight.suggested_wait(six["median"], 30)
    assert slow["GET FTP_SETTINGS"]["suggest_wait"] is None
    # Each command by firmware (10-10-2026): 7.4.27C answered nothing — marked, a lead to follow.
    builds = {f["firmware"]: f for f in slow["GET FTP_SETTINGS"]["firmware"]}
    assert (builds["7.5.1A"]["sent"], builds["7.5.1A"]["answered"], builds["7.5.1A"]["pct"]) == (4, 3, 75)
    assert (builds["7.4.27C"]["pct"], builds["7.4.27C"]["low"], builds["7.5.1A"]["low"]) == (0, True, False)
    # The whole day on the axis, always (10-10-2026): one busy hour is one column of 24.
    hours = o["hours"]
    assert [h["hour"] for h in hours] == [f"{n:02d}" for n in range(24)]
    assert sum(h["current"] for h in hours) == 1 and sum(h["total"] for h in hours) == 12
    assert all(h["future"] == (int(h["hour"]) > now.hour) for h in hours)
    attention = {a["label"]: a["value"] for a in o["attention"]}
    assert attention == {"Online but not answering": 1, "Slower than the answer wait": 1,
                         "Answered with a failure": 2, "Firmware with no answers": 1}


def test_each_hour_says_how_many_answered_and_failed():
    assert cota_insight.suggested_wait(49, 30) == 60          # GET GREEN DRIVING, 08-10-2026
    assert cota_insight.suggested_wait(20, 30) is None
    assert cota_insight.suggested_wait(None, 30) is None
