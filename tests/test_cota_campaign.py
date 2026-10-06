"""Jobs — many devices, a sequence each — against a simulated fleet and cloud.

The cloud here answers like the real one: a send takes a list of device ids and creates one record
per device; a check answers for one device. Each device is scripted — answers, sleeps, misses the
reply, fails, is lost — and time is simulated, so a job over 3,000 devices runs in seconds with
every wait its real length. The commands are the user's own list; the live pair is 14906 and 786.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta

import pytest

from ota_analytics import cota, cota_campaign, cota_run, db

GET_FTP, GET_6C0A, CLR_SOS, SET_6C0A, SET_6B38 = (
    "DAD76F4B", "DAD76C0A", "DDD76D66",
    "DBD76C0AD531D931D9322E35D9332E35D9332E35D931D93130D933",
    "DBD76B38D530303030303030303030D930303030303030303030D930303030303030303030"
    "D930303030303030303030D930303030303030303030")
FIVE = "\n".join([GET_FTP, GET_6C0A, CLR_SOS, SET_6C0A, SET_6B38])
BASE = datetime(2026, 10, 6, 9, 0, 0)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.hooks = []

    def sleep(self, seconds):
        self.t += seconds
        for hook in list(self.hooks):
            if self.t >= hook[0]:
                self.hooks.remove(hook)
                hook[1]()

    def wall(self):
        return BASE + timedelta(seconds=self.t)


class Fleet:
    """The cloud, and every device behind it. `how(device, value, n)` decides how the n-th send
    of `value` to `device` goes: "answer:20", "fail:20", "note:15", "pending", "lost"."""

    def __init__(self, clock, how=None):
        self.clock = clock
        self.how = how or (lambda device, value, n: "answer:20")
        self.records = defaultdict(list)
        self.sent = defaultdict(int)               # (device, value) → sends
        self.send_calls, self.poll_calls = [], 0
        self.fail_sends = self.fail_polls = self.reject_sends = 0
        self.n = 0

    def send(self, payload):
        ids, value = payload["deviceList"], payload["val1"]
        self.send_calls.append((self.clock.t, value, list(ids)))
        if self.reject_sends:
            self.reject_sends -= 1
            raise cota.CotaError("The cloud rejected the token (HTTP 401). It has most likely "
                                 "expired — sign in again with a fresh token from the portal.")
        if self.fail_sends:
            self.fail_sends -= 1
            return 500, '{"error": "internal"}'
        for device in ids:
            n = self.sent[(device, value)]
            self.sent[(device, value)] += 1
            kind, _, arg = self.how(device, value, n).partition(":")
            self.n += 1
            if kind != "lost":
                self.records[device].append({
                    "id": f"{device}_{self.n:08X}", "deviceId": device,
                    "timestamp": int(self.clock.wall().timestamp()), "type": payload["type"],
                    "val1": "245A" + value, "status": 0, "response": None, "responseTime": None,
                    "imei": f"86551008{device:07d}", "_how": kind, "_after": float(arg or 0),
                    "_t0": self.clock.t})
        return 200, '{"msg":"Command send Successfully.."}'

    def responses(self, device_id, start, end):
        self.poll_calls += 1
        if self.fail_polls:
            self.fail_polls -= 1
            return 503, "busy"
        out = []
        for r in self.records[device_id]:
            rec = {k: v for k, v in r.items() if not k.startswith("_")}
            if self.clock.t - r["_t0"] >= r["_after"]:
                if r["_how"] == "answer":
                    rec.update(status=1, response=f"(OK {r['val1'][4:12]})*EF")
                elif r["_how"] == "fail":
                    rec.update(status=1, response="SET X:FAIL FAILED#123")
                elif r["_how"] == "note":
                    rec.update(status=1, response="Command sent at 2026-10-06 09:00:05")
            out.append(rec)
        return 200, json.dumps({"data": out})

    def close(self):
        pass

    def sends_to(self, device):
        return [v for _, v, ids in self.send_calls if device in ids]


@pytest.fixture
def fleet(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(cota, "_now", lambda: clock.wall().strftime(cota.TIME_FORMAT))
    started = []
    monkeypatch.setattr(cota_campaign, "start", lambda cid: started.append(cid))
    monkeypatch.setattr(cota_campaign, "_threads", {})
    monkeypatch.setattr(cota_run, "start", lambda rid: None)
    conn = db.connect()

    def run(devices, text=FIVE, how=None, cloud=None, campaign_id=None, renew=lambda: False, **opts):
        cloud = cloud or Fleet(clock, how)
        cid = campaign_id or cota_campaign.create(conn, name="test", device_ids=devices,
                                                  commands_text=text, **opts)
        cota_campaign.Scheduler(cid, client_factory=lambda: cloud, renew=renew,
                                sleep=clock.sleep).run()
        return cota_campaign.summary(conn, cid), cloud

    run.clock, run.conn, run.started = clock, conn, started
    return run


def _results(conn, cid):
    return {(r["device_id"], r["step"]): dict(r) for r in conn.execute(
        "SELECT * FROM cota_campaign_result WHERE campaign_id = ?", (cid,))}


# ── groups ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, ids", [
    ("786,14906", [786, 14906]),
    ("786, 14906\n14906", [786, 14906]),                       # a duplicate is dropped
    ("Device ID\n786\n14906\n", [786, 14906]),                 # a CSV header is skipped
    ("786;14906 15000", [786, 14906, 15000]),
])
def test_device_ids_from_a_list_or_a_csv(text, ids):
    assert cota_campaign.parse_device_ids(text)[0] == ids


def test_a_bad_device_id_is_named_by_its_line():
    ids, problems, dupes = cota_campaign.parse_device_ids("786\nabc\n786")
    assert ids == [786] and dupes == 1 and problems == ["line 2: 'abc' is not a device id"]


def test_a_csv_upload_reads_the_device_id_column():
    content = b"\xef\xbb\xbfName,Device ID\nbus 1,786\nbus 2,14906\n"
    assert cota_campaign.parse_device_ids(cota_campaign.read_csv_ids(content))[0] == [786, 14906]


def test_a_group_keeps_its_order_and_can_be_replaced(fleet):
    gid = cota_campaign.save_group(fleet.conn, "Desk", [14906, 786])
    assert cota_campaign.group_devices(fleet.conn, gid) == [14906, 786]
    assert cota_campaign.save_group(fleet.conn, "Desk", [786]) == gid
    assert cota_campaign.group_devices(fleet.conn, gid) == [786]
    assert [g["devices"] for g in cota_campaign.groups(fleet.conn)] == [1]


# ── the live pair, simulated ──────────────────────────────────────────────

def test_two_devices_five_commands_one_call_per_command(fleet):
    job, cloud = fleet([14906, 786])
    assert job["state"] == "done" and job["results"] == {"done": 10}
    assert [ids for _, _, ids in cloud.send_calls] == [[14906, 786]] * 5   # both in each call
    assert [v for _, v, _ in cloud.send_calls] == [GET_FTP, GET_6C0A, CLR_SOS, SET_6C0A, SET_6B38]
    # The per-device conversation is the console's: all five, answered, for each device.
    for device in (14906, 786):
        th = cota.thread(fleet.conn, device)
        assert [c["params"]["val1"] for c in th["entries"]] == [GET_FTP, GET_6C0A, CLR_SOS,
                                                                 SET_6C0A, SET_6B38]
        assert {c["stage"] for c in th["entries"]} == {"device"}


def test_each_device_moves_at_its_own_pace(fleet):
    slow = lambda device, value, n: "answer:90" if device == 786 and value == GET_FTP else "answer:20"
    job, cloud = fleet([14906, 786], text=f"{GET_FTP}\n{GET_6C0A}", how=slow)
    assert job["state"] == "done"
    first_786_answer = next(t for t, v, ids in cloud.send_calls if v == GET_6C0A and 786 in ids)
    first_14906_next = next(t for t, v, ids in cloud.send_calls if v == GET_6C0A and 14906 in ids)
    assert first_14906_next < first_786_answer                 # 14906 did not wait for 786


def test_one_at_a_time_per_device(fleet):
    job, cloud = fleet([14906, 786])
    for device in (14906, 786):
        rows = [r for r in fleet.conn.execute(
            "SELECT t.sent_at, c.device_response_at FROM cota_task t JOIN cota_command c "
            "ON c.task_id = t.id WHERE t.device_id = ? ORDER BY t.id", (device,))]
        for earlier, later in zip(rows, rows[1:]):
            assert earlier["device_response_at"] <= later["sent_at"]


# ── batching, waves and the automatic stop ────────────────────────────────

def test_a_large_group_is_sent_in_batches_after_its_canary(fleet):
    devices = list(range(1000, 1120))                          # 120 devices
    job, cloud = fleet(devices, batch_size=50, rate_per_sec=50)
    assert job["state"] == "done" and job["results"] == {"done": 600}
    assert job["canary_size"] == 2
    sizes = [len(ids) for _, _, ids in cloud.send_calls]
    assert sizes[:5] == [2] * 5                                # the canary's five commands
    assert max(sizes) <= 50 and sum(sizes) == 600              # never over the batch size
    assert len(sizes) <= 5 + 2 * 15                            # far fewer calls than devices
    canary_done = max(t for t, _, ids in cloud.send_calls if ids == devices[:2])
    rest_first = min(t for t, _, ids in cloud.send_calls if devices[2] in ids)
    assert rest_first > canary_done                            # the rest waited for the canary


def test_a_failing_canary_stops_the_job_before_the_rest(fleet):
    devices = list(range(2000, 2030))                          # 30 devices: canary of 1
    fails = lambda d, v, n: "fail:20"
    job, cloud = fleet(devices, text=GET_FTP, how=fails, rate_per_sec=50)
    assert job["state"] == "paused" and "canary failed" in job["pause_reason"]
    assert {i for _, _, ids in cloud.send_calls for i in ids} == {2000}   # nobody else touched


def test_resuming_after_the_canary_sends_to_the_rest_and_the_stop_still_guards(fleet):
    devices = list(range(2000, 2030))
    fails = lambda d, v, n: "fail:20"
    job, cloud = fleet(devices, text=GET_FTP, how=fails, rate_per_sec=50)
    cota_campaign.request(fleet.conn, job["id"], "resume")
    assert fleet.started == [job["id"]]
    job, _ = fleet([], cloud=cloud, campaign_id=job["id"])
    assert job["state"] == "paused" and "above the 10% limit" in job["pause_reason"]
    assert job["results"].get("failed", 0) >= cota_campaign.FAIL_SAMPLE


# ── the failure cases, at fleet scale ─────────────────────────────────────

def test_a_sleeping_device_is_not_resent_and_expires(fleet):
    asleep = lambda d, v, n: "pending" if d == 786 else "answer:20"
    job, cloud = fleet([14906, 786], text=f"{GET_FTP}\n{GET_6C0A}", how=asleep, validity_hours=1)
    assert job["state"] == "done"
    assert cloud.sends_to(786) == [GET_FTP]                    # sent once, never again
    results = _results(fleet.conn, job["id"])
    assert results[(786, 0)]["state"] == "expired" and results[(786, 1)]["state"] == "skipped"
    assert results[(14906, 1)]["state"] == "done"


def test_a_sleeping_device_is_checked_less_and_less_and_its_reply_stored_once(fleet):
    asleep = lambda d, v, n: "pending"
    job, cloud = fleet([786], text=GET_FTP, how=asleep, validity_hours=1)
    # Every 10 s for 2 min, every minute to 10 min, every 5 min to the hour: ~30 checks, not 360.
    assert 25 <= cloud.poll_calls <= 35
    row = fleet.conn.execute("SELECT first_seen_at, last_seen_at FROM cota_command").fetchone()
    assert row["first_seen_at"] == row["last_seen_at"]         # the same answer was not rewritten
    assert fleet.conn.execute("SELECT COUNT(*) FROM cota_poll").fetchone()[0] == 0


def test_delivered_but_unanswered_is_retried_three_times_then_the_next_command(fleet):
    note = lambda d, v, n: "note:15" if v == GET_FTP else "answer:20"
    job, cloud = fleet([786], text=f"{GET_FTP}\n{GET_6C0A}", how=note)
    results = _results(fleet.conn, job["id"])
    assert results[(786, 0)]["state"] == "failed" and results[(786, 0)]["attempts"] == 3
    assert results[(786, 0)]["outcome"] == "delivered_no_answer"
    assert results[(786, 1)]["state"] == "done"
    assert cloud.sends_to(786) == [GET_FTP] * 3 + [GET_6C0A]


def test_a_lost_command_is_resent(fleet):
    lost_once = lambda d, v, n: "lost" if n == 0 else "answer:20"
    job, cloud = fleet([786], text=GET_FTP, how=lost_once)
    assert job["results"] == {"done": 1} and cloud.sends_to(786) == [GET_FTP, GET_FTP]


def test_a_failed_answer_is_retried_then_given_up(fleet):
    job, cloud = fleet([786], text=f"{SET_6C0A}\n{GET_6C0A}",
                       how=lambda d, v, n: "fail:20" if v == SET_6C0A else "answer:20")
    results = _results(fleet.conn, job["id"])
    assert results[(786, 0)]["state"] == "failed" and results[(786, 0)]["attempts"] == 3
    assert results[(786, 1)]["state"] == "done"


def test_the_api_down_for_a_batch_is_retried(fleet):
    cloud = Fleet(fleet.clock)
    cloud.fail_sends = 2
    job, _ = fleet([14906, 786], text=GET_FTP, cloud=cloud)
    assert job["results"] == {"done": 2}
    assert len(cloud.send_calls) == 3                          # two refused, one accepted


def test_the_api_down_three_times_in_a_row_pauses_and_resumes(fleet):
    cloud = Fleet(fleet.clock)
    cloud.fail_sends = 3
    job, _ = fleet([14906, 786], text=GET_FTP, cloud=cloud)
    assert job["state"] == "paused" and "did not accept 3 calls" in job["pause_reason"]
    cota_campaign.request(fleet.conn, job["id"], "resume")
    job, _ = fleet([], cloud=cloud, campaign_id=job["id"])
    assert job["state"] == "done" and job["results"] == {"done": 2}


def test_checks_failing_three_times_pause_the_job(fleet):
    cloud = Fleet(fleet.clock)
    fleet.clock.hooks.append((5, lambda: setattr(cloud, "fail_polls", 3)))
    job, _ = fleet([14906], text=GET_FTP, cloud=cloud)
    assert job["state"] == "paused" and "did not answer 3 checks" in job["pause_reason"]


def test_an_expired_session_renews_or_pauses(fleet):
    cloud = Fleet(fleet.clock)
    cloud.reject_sends = 1
    renewed = []
    job, _ = fleet([14906, 786], text=GET_FTP, cloud=cloud, renew=lambda: renewed.append(1) or True)
    assert job["state"] == "done" and renewed == [1]
    cloud2 = Fleet(fleet.clock)
    cloud2.reject_sends = 1
    job, _ = fleet([555], text=GET_FTP, cloud=cloud2)
    assert job["state"] == "paused" and "Session expired" in job["pause_reason"]


def test_cancel_stops_everything_left(fleet):
    cid = cota_campaign.create(fleet.conn, name="t", device_ids=[14906, 786], commands_text=FIVE)
    fleet.clock.hooks.append((30, lambda: cota_campaign.request(fleet.conn, cid, "cancel")))
    job, cloud = fleet([], campaign_id=cid)
    assert job["state"] == "cancelled"
    assert set(job["results"]) <= {"done", "cancelled"} and job["results"]["cancelled"] >= 8


def test_a_job_left_running_by_a_restart_comes_back_paused(fleet):
    cid = cota_campaign.create(fleet.conn, name="t", device_ids=[14906], commands_text=GET_FTP)
    assert cota_campaign.recover(fleet.conn) == 1
    assert cota_campaign.summary(fleet.conn, cid)["state"] == "paused"


# ── one thing at a time per device ────────────────────────────────────────

def test_only_one_job_at_a_time_and_no_device_in_two_things(fleet):
    cota_campaign.create(fleet.conn, name="a", device_ids=[14906, 786], commands_text=GET_FTP)
    with pytest.raises(cota_campaign.CampaignError, match="already running"):
        cota_campaign.create(fleet.conn, name="b", device_ids=[555], commands_text=GET_FTP)
    with pytest.raises(cota_run.RunError, match="in a running job"):
        cota_run.create_run(fleet.conn, 786, 124, 36, GET_FTP)


def test_a_device_in_a_live_sequence_cannot_join_a_job(fleet):
    cota_run.create_run(fleet.conn, 786, 124, 36, GET_FTP)
    with pytest.raises(cota_campaign.CampaignError, match="one thing at a time"):
        cota_campaign.create(fleet.conn, name="a", device_ids=[14906, 786], commands_text=GET_FTP)


def test_bad_commands_stop_the_job_before_it_starts(fleet):
    with pytest.raises(cota_campaign.CampaignError, match="line 2"):
        cota_campaign.create(fleet.conn, name="a", device_ids=[786], commands_text="DAD76F4B\nzz")


# ── the plan, the export, and the day's record ────────────────────────────

def test_the_plan_preview_counts_calls_before_anything_is_sent(fleet):
    p = cota_campaign.plan(fleet.conn, list(range(30000)), FIVE, batch_size=50)
    assert p["devices"] == 30000 and p["send_calls"] == 600 * 5 and p["canary"] == 20
    assert p["names"] == ["GET FTP_SETTINGS", "GET 6C0A", "CLR SOS", "SET 6C0A", "SET 6B38"]
    assert cota_campaign.plan(fleet.conn, [786], FIVE, batch_size=1000)["send_calls"] == 5


def test_the_export_is_one_row_per_device_and_command(fleet):
    job, _ = fleet([14906, 786], text=f"{GET_FTP}\n{CLR_SOS}")
    rows = list(cota_campaign.export_rows(fleet.conn, job["id"]))
    assert [(r["device_id"], r["step"], r["name"], r["state"]) for r in rows] == [
        (786, 1, "GET FTP_SETTINGS", "Answered"), (786, 2, "CLR SOS", "Answered"),
        (14906, 1, "GET FTP_SETTINGS", "Answered"), (14906, 2, "CLR SOS", "Answered")]
    assert rows[0]["answer"].startswith("(OK ")


def test_the_grid_shows_each_command_by_outcome(fleet):
    asleep = lambda d, v, n: "pending" if d == 786 else "answer:20"
    job, _ = fleet([14906, 786], text=f"{GET_FTP}\n{GET_6C0A}", how=asleep, validity_hours=1)
    grid = cota_campaign.grid(fleet.conn, job["id"])
    assert grid[0]["counts"] == {"done": 1, "expired": 1}
    assert grid[1]["counts"] == {"done": 1, "skipped": 1}


def test_a_new_days_session_clears_yesterday_but_keeps_groups_and_the_map(fleet, monkeypatch):
    conn = fleet.conn
    gid = cota_campaign.save_group(conn, "Desk", [14906, 786])
    with conn:
        conn.execute("INSERT INTO cota_device VALUES ('865510083360422', 14906, 124, 'x', 'y')")
        conn.execute("INSERT INTO cota_job (name, source_file, created_at) "
                     "VALUES ('old', 'console:14906', '2026-10-05 18:00:00')")
        conn.execute("INSERT INTO cota_job (name, source_file, created_at) "
                     "VALUES ('new', 'console:14906', '2026-10-06 08:00:00')")
    monkeypatch.setattr(cota_campaign, "_purged_this_process", False)
    assert cota_campaign.purge_previous_days(conn, today=datetime(2026, 10, 6, 9, 0)) == 1
    assert [r[0] for r in conn.execute("SELECT name FROM cota_job")] == ["new"]
    assert cota_campaign.group_devices(conn, gid) == [14906, 786]
    assert conn.execute("SELECT COUNT(*) FROM cota_device").fetchone()[0] == 1
    # Once per process: a session that runs past midnight keeps its day.
    assert cota_campaign.purge_previous_days(conn, today=datetime(2026, 10, 7, 9, 0)) == 0


def test_a_day_whose_only_record_is_a_job_is_still_cleared(fleet, monkeypatch):
    """The purge used to look only at sends and cloud records, so yesterday's job — whose own
    sends sit under a job of their own — was kept if nothing else from that day was."""
    conn = fleet.conn
    with conn:
        conn.execute("""
            INSERT INTO cota_campaign (name, device_type, cmd_type, commands, batch_size, rate_per_sec,
                validity_hours, canary_size, max_fail_share, state, created_at)
            VALUES ('old', 124, 36, '["DAD76F4B"]', 50, 5, 12, 0, 0.1, 'done', '2026-10-05 18:00:00')
        """)
    monkeypatch.setattr(cota_campaign, "_purged_this_process", False)
    assert cota_campaign.purge_previous_days(conn, today=datetime(2026, 10, 6, 9, 0)) == 1
    assert conn.execute("SELECT COUNT(*) FROM cota_campaign").fetchone()[0] == 0


# ── scale ─────────────────────────────────────────────────────────────────

SCALE = int(os.environ.get("OTA_SCALE_DEVICES", "1000"))


def test_scale_a_fleet_job_in_calls_rows_and_time(fleet):
    """1,000 devices × 5 commands by default; OTA_SCALE_DEVICES=30000 for the full fleet."""
    devices = list(range(100000, 100000 + SCALE))
    began = time.perf_counter()
    job, cloud = fleet(devices, batch_size=1000, rate_per_sec=200)
    elapsed = time.perf_counter() - began
    canary = cota_campaign.canary_size(SCALE)
    ideal = 5 + 5 * math.ceil((SCALE - canary) / 1000)
    assert job["state"] == "done" and job["results"] == {"done": SCALE * 5}
    # Answers to a big batch are found over several ticks (checks are paced), so a command's next
    # step can go in a few calls rather than one — but never one call per device.
    assert ideal <= len(cloud.send_calls) <= 3 * ideal
    rows = fleet.conn.execute("SELECT COUNT(*) FROM cota_task").fetchone()[0]
    assert rows == SCALE * 5                                    # one task per device per command
    print(f"\n  {SCALE} devices × 5: {len(cloud.send_calls)} sends, {cloud.poll_calls} checks, "
          f"{rows} tasks, simulated {fleet.clock.t / 60:.0f} min, {elapsed:.1f}s real")
