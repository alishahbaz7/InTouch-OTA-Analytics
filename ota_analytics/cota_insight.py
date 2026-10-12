"""What today's Intouch COTA record says — the Overview page.

Every figure is counted from this install's own record of the day (the COTA record lives for a
day), plus Web FOTA's device registry for the devices that never answered: the cloud's records
name each device's IMEI, and the registry knows when that IMEI last pinged the OTA platform. A
device that never answers a command but pinged the platform in the last hour is the COTA
counterpart of Web FOTA's "Task pending — Online": powered, connected, and still silent — the
one worth chasing. The rest is the field: switched off, out of coverage.

Nothing here calls the cloud, and nothing writes.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime

from . import cota, cota_campaign, cota_run

# Why a command failed, in the order a person would chase them, with the palette's tone for each:
# a held command is the field (orange: waiting by design), a silent or refused one is a fault.
REASONS = [
    ("not_delivered", "Held by the cloud", "seg-orange"),         # the device was not connected
    ("delivered_no_answer", "Delivered, no reply", "seg-bad"),
    ("answered_failure", "Answered with a failure", "seg-bad"),
    ("not_in_cloud", "Never listed by the cloud", "seg-dim"),
    ("not_accepted", "Refused by the cloud", "seg-bad"),
]

# What Web FOTA says about a device that never answered today, freshest first.
PING_BUCKETS = [
    ("hour", "Pinged in the last hour", "seg-yellow"),
    ("day", "Pinged today", "seg-orange"),
    ("old", "Silent over a day", "seg-dim"),
    ("never", "Never pinged", "seg-dim"),
    ("unknown", "Not in Web FOTA", "seg-2"),
]


def _ping_bucket(seen_at: str | None, now: datetime) -> tuple[str, float | None]:
    if not seen_at:
        return "never", None
    try:
        hours = (now - datetime.strptime(seen_at[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
    except ValueError:
        return "never", None
    return ("hour" if hours < 1 else "day" if hours < 24 else "old"), hours


def _median(values: list[float]) -> float | None:
    v = sorted(values)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def suggested_wait(median_seconds: float | None, wait: float) -> int | None:
    """A wait that would catch a slow command's typical answer on the first attempt — a fifth
    over its median, in steps of 15 s — or None when the job's wait already does."""
    if median_seconds is None or median_seconds <= wait:
        return None
    return int(math.ceil(median_seconds * 1.2 / 15) * 15)


def by_hour(conn, now: datetime | None = None) -> list[dict]:
    """Answered and failed commands per hour today, with each hour's answer rate."""
    now = now or cota._parse(cota._now())
    rows = {r[0]: (r[1], r[2]) for r in conn.execute("""
        SELECT CAST(substr(finished_at, 12, 2) AS INTEGER), SUM(state = 'done'), SUM(state = 'failed')
        FROM cota_campaign_result WHERE finished_at IS NOT NULL AND state IN ('done', 'failed')
        GROUP BY 1
    """)}
    if not rows:
        return []
    # The whole day, always: one busy hour is then one slim column among 24, not a block that
    # fills the chart (the user, 10-10-2026).
    out = []
    for h in range(24):
        answered, failed = rows.get(h, (0, 0))
        done = answered + failed
        out.append({"hour": f"{h:02d}", "answered": answered, "failed": failed, "total": done,
                    "rate": round(100 * answered / done) if done else None,
                    "current": h == now.hour, "future": h > now.hour})
    return out


def _device_outcomes(conn) -> dict[int, dict]:
    """Each device sent anything today: commands answered, failed, and still to finish."""
    return {r["device_id"]: dict(r) for r in conn.execute("""
        SELECT device_id,
               SUM(state = 'done' OR outcome = 'answered_failure') AS heard,
               SUM(state = 'done') AS answered, SUM(state = 'failed') AS failed,
               SUM(state = 'queued') AS open
        FROM cota_campaign_result WHERE attempts > 0 GROUP BY device_id
    """)}


def _devices_with_fota(conn) -> list[dict]:
    """Each device sent anything today, what it did, its IMEI — from the device map, else the
    cloud's newest record — and Web FOTA's registry row for that IMEI. One statement: a fleet
    job is 30,000 devices, and a query per device would make the page wait for each."""
    return [dict(r) for r in conn.execute("""
        WITH d AS (
            SELECT device_id, SUM(state = 'done' OR outcome = 'answered_failure') AS heard,
                   SUM(state = 'done') AS answered, SUM(state = 'failed') AS failed,
                   SUM(state = 'queued') AS open
            FROM cota_campaign_result WHERE attempts > 0 GROUP BY device_id),
        i AS (
            SELECT d.*, COALESCE(
                (SELECT imei FROM cota_device m WHERE m.device_id = d.device_id LIMIT 1),
                (SELECT imei FROM cota_command c WHERE c.device_id = d.device_id AND c.imei <> ''
                 ORDER BY c.last_seen_at DESC LIMIT 1)) AS imei
            FROM d)
        SELECT i.*, r.imei IS NOT NULL AS in_fota, r.seen_at, r.firmware, r.device_model
        FROM i LEFT JOIN device r ON r.imei = i.imei
    """)]


def silent_devices(conn, now: datetime | None = None) -> dict:
    """Devices that failed commands today and answered none — and Web FOTA's view of each."""
    now = now or datetime.now()
    rows = _devices_with_fota(conn)
    buckets = Counter()
    online, firmware = [], {}
    silent = 0
    for d in rows:
        # Answer rate by model and firmware, over every device sent anything today.
        key = ((d["device_model"] or "(unknown)", d["firmware"] or "(unknown)") if d["in_fota"]
               else ("Not in Web FOTA", ""))
        f = firmware.setdefault(key, {"model": key[0], "firmware": key[1], "devices": 0,
                                      "all": 0, "some": 0, "none": 0})
        f["devices"] += 1
        if d["heard"] and not d["failed"]:
            f["all"] += 1
        elif d["heard"]:
            f["some"] += 1
        elif d["failed"]:
            f["none"] += 1
        if d["heard"] or not d["failed"]:
            continue
        silent += 1
        if not d["in_fota"]:
            buckets["unknown"] += 1
            continue
        bucket, hours = _ping_bucket(d["seen_at"], now)
        buckets[bucket] += 1
        if bucket == "hour":
            online.append({"device_id": d["device_id"], "imei": d["imei"], "model": d["device_model"],
                           "firmware": d["firmware"], "seen_minutes": round(hours * 60),
                           "failed": d["failed"]})
    for o in online:
        o["last_sent"] = conn.execute("SELECT MAX(sent_at) FROM cota_task WHERE device_id = ?",
                                      (o["device_id"],)).fetchone()[0]
    online.sort(key=lambda d: d["seen_minutes"])
    return {
        "total": silent,
        "buckets": [{"key": k, "label": label, "tone": tone, "value": buckets.get(k, 0)}
                    for k, label, tone in PING_BUCKETS if buckets.get(k) or k in ("hour", "day", "old")],
        "online": online,
        "firmware": sorted(firmware.values(), key=lambda f: -f["devices"]),
        "fota_as_of": conn.execute("SELECT MAX(snapshot_at) FROM snapshot").fetchone()[0],
    }


def device_split(conn) -> list[dict]:
    """Today's devices by what they did: answered everything, some, nothing — or still going."""
    split = Counter()
    for o in _device_outcomes(conn).values():
        if o["heard"] and not o["failed"] and not o["open"]:
            split["all"] += 1
        elif o["heard"]:
            split["some"] += 1
        elif o["failed"]:
            split["none"] += 1
        else:
            split["open"] += 1
    return [{"label": "Answered everything", "value": split["all"], "tone": "seg-ok"},
            {"label": "Answered some", "value": split["some"], "tone": "seg-warn"},
            {"label": "Answered nothing", "value": split["none"], "tone": "seg-dim"},
            {"label": "Still in progress", "value": split["open"], "tone": "seg-0"}]


def command_split(conn) -> list[dict]:
    """Every device command today, once each: answered, failed, out, waiting to go, stopped."""
    r = {row[0]: row[1] for row in conn.execute(
        "SELECT CASE WHEN state = 'queued' AND attempts > 0 THEN 'out' ELSE state END, COUNT(*) "
        "FROM cota_campaign_result GROUP BY 1")}
    return [{"label": "Answered", "value": r.get("done", 0), "tone": "seg-ok"},
            {"label": "Failed", "value": r.get("failed", 0), "tone": "seg-bad"},
            {"label": "Out, waiting for an answer", "value": r.get("out", 0), "tone": "seg-yellow"},
            {"label": "Not sent yet", "value": r.get("queued", 0), "tone": "seg-0"},
            {"label": "Stopped — cancelled, expired, not started",
             "value": sum(r.get(k, 0) for k in ("cancelled", "expired", "skipped")), "tone": "seg-dim"}]


def failure_reasons(conn) -> dict:
    """Why today's commands failed, the ones rescued by a late answer, and the resends spent."""
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT outcome, COUNT(*) FROM cota_campaign_result WHERE state = 'failed' GROUP BY outcome")}
    known = {k for k, _, _ in REASONS}
    rows = [{"key": k, "label": label, "tone": tone, "value": counts.get(k, 0)}
            for k, label, tone in REASONS if counts.get(k)]
    other = sum(v for k, v in counts.items() if k not in known)
    if other:
        rows.append({"key": "other", "label": "Other", "tone": "seg-dim", "value": other})
    resends = conn.execute("SELECT COALESCE(SUM(attempts - 1), 0) FROM cota_campaign_result "
                           "WHERE attempts > 1").fetchone()[0]
    wasted = conn.execute("""
        SELECT COALESCE(SUM(attempts - 1), 0) FROM cota_campaign_result
        WHERE attempts > 1 AND device_id IN (
            SELECT device_id FROM cota_campaign_result WHERE attempts > 0 GROUP BY device_id
            HAVING SUM(state = 'done' OR outcome = 'answered_failure') = 0 AND SUM(state = 'failed') > 0)
    """).fetchone()[0]
    late = conn.execute("SELECT COUNT(*) FROM cota_campaign_result WHERE outcome = 'answered_late'"
                        ).fetchone()[0]
    return {"rows": rows, "failed": sum(counts.values()), "late": late,
            "resends": resends, "resends_to_silent": wasted}


def command_health(conn) -> list[dict]:
    """One row per command sent today, across every job: how it went, how fast, and why not."""
    stats: dict[str, dict] = {}
    # Each device's firmware in Web FOTA, by its IMEI (the Devices list, else the cloud's newest
    # record): answers beside firmware show which builds answer a command (the user, 10-10-2026).
    for r in conn.execute("""
        WITH fw AS (
            SELECT d.device_id, f.firmware
            FROM (SELECT DISTINCT device_id FROM cota_campaign_result WHERE attempts > 0) d
            LEFT JOIN device f ON f.imei = COALESCE(
                (SELECT imei FROM cota_device m WHERE m.device_id = d.device_id),
                (SELECT imei FROM cota_command k WHERE k.device_id = d.device_id AND k.imei <> ''
                 ORDER BY k.last_seen_at DESC LIMIT 1)))
        SELECT json_extract(c.commands, '$[' || r.step || ']') AS value, r.state, r.outcome,
               r.answered_attempt, r.answer_seconds, c.answer_wait_seconds AS wait, fw.firmware
        FROM cota_campaign_result r JOIN cota_campaign c ON c.id = r.campaign_id
        LEFT JOIN fw ON fw.device_id = r.device_id
        WHERE r.attempts > 0
    """):
        s = stats.setdefault(r["value"], {"value": r["value"], "sent": 0, "answered": 0, "first": 0,
                                          "times": [], "failures": Counter(), "waits": Counter(),
                                          "by_fw": {}})
        f = s["by_fw"].setdefault(r["firmware"] or "", {"sent": 0, "answered": 0, "failed": 0})
        f["sent"] += 1
        f["answered"] += r["state"] == "done"
        f["failed"] += r["state"] == "failed"
        s["sent"] += 1
        s["waits"][r["wait"] or cota_run.ANSWER_WAIT_SECONDS] += 1
        if r["state"] == "done":
            s["answered"] += 1
            s["first"] += r["answered_attempt"] == 1
            if r["answer_seconds"] is not None:
                s["times"].append(r["answer_seconds"])
        elif r["state"] == "failed":
            s["failures"][r["outcome"]] += 1
    words = {k: label for k, label, _ in REASONS}
    out = []
    for s in stats.values():
        wait = s["waits"].most_common(1)[0][0]
        median = _median(s["times"])
        top = s["failures"].most_common(1)
        out.append({
            "value": s["value"], "name": cota.describe_command(s["value"]) or s["value"][:16],
            "sent": s["sent"], "answered": s["answered"],
            "answered_pct": round(100 * s["answered"] / s["sent"]) if s["sent"] else 0,
            "first_pct": round(100 * s["first"] / s["answered"]) if s["answered"] else None,
            "median": median, "slowest": max(s["times"]) if s["times"] else None,
            "failed": sum(s["failures"].values()),
            "main_failure": (words.get(top[0][0], cota_run.OUTCOME_WORDS.get(top[0][0], top[0][0])),
                             top[0][1]) if top else None,
            "wait": wait, "suggest_wait": suggested_wait(median, wait),
            "firmware": _by_firmware(s),
        })
    return sorted(out, key=lambda c: (-c["sent"], c["name"]))


def _by_firmware(s: dict) -> list[dict]:
    """A command's results per firmware, most sent first. A build answering far less often than
    the command does overall is marked — a lead to follow, not a verdict."""
    overall = s["answered"] / s["sent"] if s["sent"] else 0
    rows = []
    for firmware, f in s["by_fw"].items():
        judged = f["answered"] + f["failed"]
        rate = f["answered"] / judged if judged else None
        rows.append({"firmware": firmware, "sent": f["sent"], "answered": f["answered"],
                     "failed": f["failed"], "pct": round(100 * rate) if rate is not None else None,
                     "low": rate is not None and f["sent"] >= 2 and rate <= overall - 0.3})
    return sorted(rows, key=lambda r: (-r["sent"], r["firmware"] == "", r["firmware"]))


def overview(conn, now: datetime | None = None) -> dict | None:
    """Everything the Overview page shows, or None when nothing was sent today."""
    today = cota_campaign.today(conn)
    if not today or not conn.execute("SELECT 1 FROM cota_campaign_result WHERE attempts > 0 "
                                     "LIMIT 1").fetchone():
        return None
    silent = silent_devices(conn, now)
    commands = command_health(conn)
    reasons = failure_reasons(conn)
    slow = [c for c in commands if c["suggest_wait"]]
    quiet_firmware = [f for f in silent["firmware"]
                      if f["devices"] >= 2 and not (f["all"] + f["some"]) and f["firmware"]]
    refused = next((r["value"] for r in reasons["rows"] if r["key"] == "answered_failure"), 0)
    held = next((r["value"] for r in reasons["rows"] if r["key"] == "not_delivered"), 0)
    attention = [
        {"icon": "!", "label": "Online but not answering", "value": len(silent["online"]),
         "alert": bool(silent["online"]), "href": "#online",
         "tip": "Never answered a command today, yet pinged the OTA platform in the last hour."},
        {"icon": "◷", "label": "Slower than the answer wait", "value": len(slow),
         "alert": bool(slow), "href": "#commands",
         "detail": ", ".join(f"{c['name']} ~{round(c['median'])} s → wait {c['suggest_wait']} s"
                             for c in slow[:2]),
         "tip": "Their typical answer comes after the wait, so most go a second time."},
        {"icon": "✕", "label": "Answered with a failure", "value": refused,
         "alert": bool(refused), "href": "#commands"},
        {"icon": "⌁", "label": "Firmware with no answers", "value": len(quiet_firmware),
         "alert": bool(quiet_firmware), "href": "#firmware",
         "detail": ", ".join(f["firmware"] for f in quiet_firmware[:3])},
    ]
    also = [
        {"icon": "…", "label": "Held by the cloud", "value": held, "href": ""},
        {"icon": "↻", "label": "Resends to devices that never answered",
         "value": reasons["resends_to_silent"], "href": ""},
        {"icon": "↺", "label": "Answered late", "value": reasons["late"], "href": ""},
    ]
    return {"today": today, "hours": by_hour(conn), "commands_donut": command_split(conn),
            "devices_donut": device_split(conn), "silent": silent, "reasons": reasons,
            "commands": commands, "attention": attention, "also": also}
