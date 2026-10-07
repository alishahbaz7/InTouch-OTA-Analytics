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
    of `value` to `device` goes: "answer:20", "fail:20", "note:15", "late:345", "pending", "lost".
    "late" is what the desk device 786 did on 07-10-2026: the cloud says "Command sent at" within
    a second, and the device's answer arrives on that same record minutes later."""

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
            if r["_how"] == "late":
                if self.clock.t - r["_t0"] >= r["_after"]:
                    rec.update(status=1, response=f"(OK {r['val1'][4:12]})*EF")
                elif self.clock.t - r["_t0"] >= 1:
                    rec.update(status=1, response="Command sent at 2026-10-06 09:00:05")
            elif self.clock.t - r["_t0"] >= r["_after"]:
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

def test_a_held_command_is_resent_like_any_unanswered_one(fleet):
    """The user's rule (2026-10-07): no answer in 30 s is no answer, whatever the cloud says it
    did with the command. A held command is not waited for — that kept a job open 12 hours."""
    asleep = lambda d, v, n: "pending" if d == 786 else "answer:20"
    job, cloud = fleet([14906, 786], text=f"{GET_FTP}\n{GET_6C0A}", how=asleep)
    assert job["state"] == "done"
    assert cloud.sends_to(786) == [GET_FTP] * 3 + [GET_6C0A] * 3     # three each, then on
    results = _results(fleet.conn, job["id"])
    assert results[(786, 0)]["state"] == "failed" and results[(786, 0)]["outcome"] == "not_delivered"
    assert results[(786, 1)]["state"] == "failed" and results[(14906, 1)]["state"] == "done"
    assert fleet.clock.t <= 2 * 120 + 30                              # two commands, ~2 min each


def test_an_unanswered_attempt_is_checked_three_times_and_its_reply_stored_once(fleet):
    job, cloud = fleet([786], text=GET_FTP, how=lambda d, v, n: "pending")
    assert 8 <= cloud.poll_calls <= 10                     # at 10, 20 and 30 s, three attempts
    for row in fleet.conn.execute("SELECT first_seen_at, last_seen_at FROM cota_command"):
        assert row["first_seen_at"] <= row["last_seen_at"]
    assert fleet.conn.execute("SELECT COUNT(*) FROM cota_poll").fetchone()[0] == 0


def test_a_long_answer_wait_is_checked_less_and_less_and_stopped_by_the_time_limit(fleet):
    job, cloud = fleet([786], text=GET_FTP, how=lambda d, v, n: "pending",
                       answer_wait_seconds=3600, time_limit_minutes=60)
    assert job["state"] == "done" and len(cloud.send_calls) == 1
    # Every 10 s for 2 min, every minute to 10 min, every 5 min to the hour: ~30 checks, not 360.
    assert 25 <= cloud.poll_calls <= 35
    r = _results(fleet.conn, job["id"])[(786, 0)]
    assert r["state"] == "expired" and r["outcome"] == "time_limit"
    assert 3600 <= fleet.clock.t <= 3600 + 2 * cota_campaign.TICK_SECONDS


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
    job, _ = fleet([14906, 786], text=f"{GET_FTP}\n{GET_6C0A}", how=asleep)
    grid = cota_campaign.grid(fleet.conn, job["id"])
    assert grid[0]["counts"] == {"done": 1, "failed": 1}
    assert grid[1]["counts"] == {"done": 1, "failed": 1}


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




# ── time to answer, and the job's own answer wait (2.0.1) ─────────────────

def test_an_answer_on_the_third_attempt_is_timed_from_the_first(fleet):
    """What the desk device did on 07-10-2026: two attempts the cloud called sent but nobody
    answered, then an answer — recorded as attempt 3, timed from attempt 1."""
    third = lambda d, v, n: "note:1" if n < 2 else "answer:20"
    job, cloud = fleet([786], text=GET_FTP, how=third)
    r = _results(fleet.conn, job["id"])[(786, 0)]
    assert r["state"] == "done" and r["attempts"] == 3 and r["answered_attempt"] == 3
    assert 2 * cota_run.ANSWER_WAIT_SECONDS < r["answer_seconds"] < 3 * cota_run.ANSWER_WAIT_SECONDS + 120
    grid = cota_campaign.grid(fleet.conn, job["id"])
    assert grid[0]["first_attempt"] == 0 and grid[0]["median_answer"] == r["answer_seconds"]


def test_an_answer_after_the_device_moved_on_is_recorded_as_answered_late(fleet):
    """786 answered minutes after the first attempt. With 30 s attempts the job has moved on by
    then — but the records read for the next command carry that answer, and it is kept."""
    # Attempts at 0, 30 and 60 s; the command gives up at 90 s. The first attempt's answer
    # comes at 95 s, while the device is on its next command.
    slow = lambda d, v, n: "late:95" if v == GET_FTP else "answer:20"
    job, cloud = fleet([786], text=f"{GET_FTP}\n{GET_6C0A}", how=slow)
    first, second = (_results(fleet.conn, job["id"])[(786, s)] for s in (0, 1))
    assert first["attempts"] == 3 and first["state"] == "done"
    assert first["outcome"] == "answered_late" and first["answered_attempt"] == 1
    assert 95 <= first["answer_seconds"] <= 110 and first["answer"].startswith("(OK ")
    assert second["state"] == "done" and second["outcome"] == "answered"


def test_an_answer_after_the_job_has_ended_is_not_waited_for(fleet):
    """The trade-off, on record: a job is fast because it does not sit waiting. An answer that
    comes after its last device has finished is in the device's conversation on Configure, not
    in the job."""
    job, cloud = fleet([786], text=GET_FTP, how=lambda d, v, n: "late:345")
    r = _results(fleet.conn, job["id"])[(786, 0)]
    assert r["state"] == "failed" and r["attempts"] == 3 and fleet.clock.t <= 110


def test_a_longer_answer_wait_lets_a_slow_device_answer_without_resends(fleet):
    late = lambda d, v, n: "late:345"
    job, cloud = fleet([786], text=GET_FTP, how=late, answer_wait_seconds=420)
    r = _results(fleet.conn, job["id"])[(786, 0)]
    assert len(cloud.send_calls) == 1 and r["attempts"] == 1 and r["answered_attempt"] == 1
    assert 345 <= r["answer_seconds"] < 345 + 61                 # seen at the next check
    assert job["answer_wait"] == 420


@pytest.mark.parametrize("typed, stored", [(None, None), ("", None), (7, 10), (45, 45),
                                           (5000, 3600)])
def test_the_answer_wait_is_kept_in_range(typed, stored):
    assert cota_campaign._answer_wait_seconds(typed) == stored


def test_a_device_conversation_shows_every_attempt_and_the_answer(fleet):
    third = lambda d, v, n: "note:1" if n < 2 else "answer:20"
    job, _ = fleet([786, 14906], text=f"{GET_FTP}\n{GET_6C0A}",
                   how=lambda d, v, n: third(d, v, n) if d == 786 and v == GET_FTP else "answer:20")
    steps = cota_campaign.device_conversation(fleet.conn, job["id"], 786)
    assert [s["name"] for s in steps] == ["GET FTP_SETTINGS", "GET 6C0A"]
    first = steps[0]
    assert [a["stage"] for a in first["attempts"]] == ["delivered", "delivered", "device"]
    assert first["attempts"][2]["answer"].startswith("(OK ") and first["answered_attempt"] == 3
    assert steps[1]["attempts"][0]["stage"] == "device" and steps[1]["answered_attempt"] == 1
    assert cota_campaign.device_conversation(fleet.conn, 999, 786) == []


def test_the_device_list_carries_serial_timing_and_the_last_answer(fleet):
    job, _ = fleet([14906, 786], text=f"{GET_FTP}\n{GET_6C0A}")
    rows, total = cota_campaign.device_rows(fleet.conn, job["id"])
    assert total == 2 and [r["serial"] for r in rows] == [1, 2]
    assert all(r["answered"] == 2 and r["median_answer"] and r["last_answer"].startswith("(OK ")
               for r in rows)
    assert rows[0]["last_reading"] == "ok"
    only, _ = cota_campaign.device_rows(fleet.conn, job["id"], search="786")
    assert [r["serial"] for r in only] == [2]                  # the job's position, not the row's


def test_rerun_takes_only_devices_that_did_not_answer_everything(fleet):
    fails = lambda d, v, n: "lost" if d == 786 and v == GET_6C0A else "answer:20"
    job, _ = fleet([14906, 786, 15000], text=f"{GET_FTP}\n{GET_6C0A}", how=fails,
                   answer_wait_seconds=60, time_limit_minutes=20)
    again = cota_campaign.draft_from(fleet.conn, job["id"], "unfinished")
    assert again["devices"] == "786" and again["from_count"] == 1
    assert again["commands"] == f"{GET_FTP}\n{GET_6C0A}" and again["answer_wait_seconds"] == "60"
    assert again["time_limit_minutes"] == "20"
    copy = cota_campaign.draft_from(fleet.conn, job["id"], "all")
    assert copy["devices"] == "14906, 786, 15000" and copy["name"].startswith(f"Copy of #{job['id']}")
    assert cota_campaign.draft_from(fleet.conn, 999) is None


def test_the_export_says_which_attempt_answered_and_when(fleet):
    third = lambda d, v, n: "note:1" if n < 2 else "answer:20"
    job, _ = fleet([786], text=GET_FTP, how=third)
    row = next(cota_campaign.export_rows(fleet.conn, job["id"]))
    assert row["answered_attempt"] == 3 and row["answer_seconds"] > 2 * cota_run.ANSWER_WAIT_SECONDS


# ── the command library ───────────────────────────────────────────────────

def test_a_parameter_name_names_every_command_on_it(fleet):
    from ota_analytics import cota_library

    assert cota.describe_command("DAD76C0A") == "GET 6C0A"
    cota_library.save_parameter(fleet.conn, "6c0a", "TIMERS")
    assert cota.describe_command("DAD76C0A") == "GET TIMERS"
    assert cota.describe_command(SET_6C0A) == "SET TIMERS"
    cota_library.save_parameter(fleet.conn, "6F4B", "FTP")              # a built-in renamed
    assert cota.describe_command(GET_FTP) == "GET FTP"
    cota_library.delete_parameter(fleet.conn, "6F4B")
    assert cota.describe_command(GET_FTP) == "GET FTP_SETTINGS"          # the built-in is back
    rows = {r["code"]: r for r in cota_library.parameters(fleet.conn)}
    assert rows["6C0A"]["own"] and rows["6F4B"]["builtin"] == "FTP_SETTINGS" and not rows["6F4B"]["own"]


def test_a_saved_command_names_that_exact_command_everywhere(fleet):
    from ota_analytics import cota_library

    cid = cota_library.save_command(fleet.conn, name="Ignition timer 1 s", val1="dbd7 6b82 d531",
                                    tags="timers, Timers ,pilot")
    assert cota.describe_command("DBD76B82D531") == "Ignition timer 1 s"
    assert cota.describe_command("DBD76B82D532") == "SET 6B82"            # a different value
    saved = cota_library.commands(fleet.conn)[0]
    assert saved["val1"] == "DBD76B82D531" and saved["tag_list"] == ["timers", "pilot"]
    assert saved["kind"] == "set" and saved["reads_as"] == "SET 6B82"
    assert cota_library.commands(fleet.conn, tag="PILOT") and not cota_library.commands(fleet.conn, q="sos")
    job, _ = fleet([786], text="DBD76B82D531")
    assert job["names"] == ["Ignition timer 1 s"]                         # the job reads it too
    cota_library.delete_command(fleet.conn, cid)
    assert cota.describe_command("DBD76B82D531") == "SET 6B82"


@pytest.mark.parametrize("name, val1, problem", [
    ("", GET_FTP, "Give the command a name"),
    ("x", "hello", "hexadecimal"),
    ("x", "DAD76F4", "odd"),
])
def test_a_saved_command_must_be_a_command(fleet, name, val1, problem):
    from ota_analytics import cota_library

    with pytest.raises(cota_library.LibraryError, match=problem):
        cota_library.save_command(fleet.conn, name=name, val1=val1)


def test_two_saved_commands_cannot_share_a_name(fleet):
    from ota_analytics import cota_library

    first = cota_library.save_command(fleet.conn, name="FTP", val1=GET_FTP)
    with pytest.raises(cota_library.LibraryError, match="already called"):
        cota_library.save_command(fleet.conn, name="FTP", val1=GET_6C0A)
    assert cota_library.save_command(fleet.conn, name="FTP", val1=GET_6C0A, command_id=first) == first


def test_a_parameter_code_is_four_hex_digits(fleet):
    from ota_analytics import cota_library

    with pytest.raises(cota_library.LibraryError, match="four hex digits"):
        cota_library.save_parameter(fleet.conn, "6C0", "x")


def test_typed_lines_are_named_as_they_are_typed():
    from ota_analytics import cota_library

    lines = cota_library.describe_lines(f"{GET_FTP}\nhello\n{CLR_SOS}")
    assert [(l["line"], l["name"]) for l in lines] == [(1, "GET FTP_SETTINGS"), (2, ""), (3, "CLR SOS")]
    assert "hexadecimal" in lines[1]["problem"]




# ── the pictures (2.0.1) ──────────────────────────────────────────────────

def test_a_jobs_answers_are_bucketed_by_time_and_attempt(fleet):
    slow = lambda d, v, n: ("note:1" if n < 2 else "answer:20") if d == 786 else "answer:20"
    job, _ = fleet([14906, 786, 15000], text=GET_FTP, how=slow)
    charts = cota_campaign.job_charts(fleet.conn, job["id"])
    times = {b["label"]: b["value"] for b in charts["answer_times"]}
    assert times["< 30 s"] == 2 and times["30 s – 2 min"] == 1      # 786 on its third attempt
    assert [a["value"] for a in charts["attempts"]] == [2, 0, 1]
    assert charts["first_try"] == 2 and charts["timed"] == 3
    assert charts["slowest_answer"] > 60 and charts["median_answer"] <= 30


def test_outcomes_count_each_command_once(fleet):
    job, _ = fleet([14906, 786], text=f"{GET_FTP}\n{GET_6C0A}",
                   how=lambda d, v, n: "lost" if d == 786 and v == GET_6C0A else "answer:20")
    segments = {s["key"]: s["value"] for s in cota_campaign.outcome_segments(job)}
    assert sum(segments.values()) == job["total"] == 4
    assert segments["done"] == 3 and segments["failed"] == 1 and segments["not_sent"] == 0


def test_a_waiting_command_is_not_also_not_sent():
    job = {"results": {"queued": 5, "done": 1}, "waiting": 2}
    segments = {s["key"]: s["value"] for s in cota_campaign.outcome_segments(job)}
    assert segments["waiting"] == 2 and segments["not_sent"] == 3


def test_today_adds_every_job_up(fleet):
    assert cota_campaign.today(fleet.conn) is None
    fleet([14906, 786], text=GET_FTP)
    fleet([786, 15000], text=GET_6C0A, how=lambda d, v, n: "lost" if d == 15000 else "answer:20")
    t = cota_campaign.today(fleet.conn)
    assert t["jobs"] == 2 and t["running"] == 0 and t["reached"] == 3
    assert t["answered"] == 3 and t["failed"] == 1 and t["answered_share"] == 75
    assert t["send_calls"] >= 2 and t["poll_calls"] > 0
    assert [j["id"] for j in t["per_job"]] == [2, 1]
    assert sum(h["value"] for h in t["per_hour"]) == 3 and t["per_hour"][-1]["current"]
    assert t["per_hour"][0]["label"] == "09"                       # the simulated morning




# ── what happens next, and when (2.0.1) ───────────────────────────────────

def _job(**over):
    return {"state": "running", "validity_hours": 12.0, "answer_wait_seconds": None,
            "commands": "[]", **over}


def _dev(**over):
    return {"state": "waiting", "step": 0, "attempt": 1, "due_at": 0, "next_poll_at": 1300.0,
            "wait_started_at": 1000.0, "step_started_at": 1000.0, **over}


SENT = {"delivered": True, "device_response": None}
HELD = {"delivered": False, "device_response": None}
KINDS = ["get", "get"]


@pytest.mark.parametrize("dev, cloud, job, what, at", [
    (_dev(state="ready", attempt=0, due_at=1500.0), None, _job(), "Sends", 1500),
    (_dev(state="ready", attempt=0, due_at=900.0), None, _job(), "Sends", 1200),     # due: now
    (_dev(state="ready", attempt=1, due_at=1500.0), None, _job(), "Resends · attempt 2", 1500),
    (_dev(wait_started_at=1190.0), SENT, _job(), "Resends · attempt 2", 1190 + 30),
    (_dev(wait_started_at=1190.0), HELD, _job(), "Resends · attempt 2", 1190 + 30),  # held = no answer
    (_dev(), SENT, _job(answer_wait_seconds=420), "Resends · attempt 2", 1000 + 420),
    (_dev(), None, _job(), "Resends · attempt 2", 1200),            # 30 s unlisted: already past
    (_dev(attempt=3, wait_started_at=1190.0), SENT, _job(), "Gives up — next command", 1220),
    (_dev(attempt=3, step=1, wait_started_at=1190.0), SENT, _job(), "Gives up — finishes", 1220),
    (_dev(), SENT, _job(), "Resends · attempt 2", 1200),             # wait over: at the next tick
    (_dev(), {"delivered": False, "device_response": "(OK)"}, _job(), "Answered — moves on", 1300),
])
def test_the_next_action_follows_the_schedulers_rules(dev, cloud, job, what, at):
    nxt = cota_campaign.next_action(job, KINDS, dev, cloud, now=1200.0)
    assert nxt["what"] == what and nxt["at"] == at


def test_nothing_is_later_than_the_jobs_time_limit():
    now = datetime(2026, 10, 7, 10, 0, 0).timestamp()
    started = datetime(2026, 10, 7, 9, 0, 20).strftime(cota.TIME_FORMAT)     # limit at 10:00:20
    job = _job(started_at=started, validity_hours=1.0)
    dev = _dev(wait_started_at=now - 5, step_started_at=now - 5)
    nxt = cota_campaign.next_action(job, KINDS, dev, SENT, now=now)
    assert nxt["what"] == "Stops — time limit" and nxt["at"] == now + 20


def test_nothing_is_next_in_a_paused_job_or_for_a_finished_device():
    assert cota_campaign.next_action(_job(state="paused"), KINDS, _dev(), SENT, now=1200.0) is None
    assert cota_campaign.next_action(_job(), KINDS, _dev(state="done"), SENT, now=1200.0) is None


def test_an_unrecognised_command_is_not_resent_once_the_cloud_sent_it():
    nxt = cota_campaign.next_action(_job(), ["unknown"], _dev(), SENT, now=1200.0)
    assert nxt["what"] == "Gives up — finishes"


def test_the_resend_comes_when_the_page_said_it_would(fleet):
    """With a 7-minute wait the checks are 60 s apart by then; the one that ends the wait is
    moved onto its boundary, so the resend comes at the wait — to the tick."""
    job, cloud = fleet([786], text=GET_FTP, how=lambda d, v, n: "note:1", answer_wait_seconds=420)
    sends = [t for t, _, _ in cloud.send_calls]
    assert len(sends) == 3
    for earlier, later in zip(sends, sends[1:]):
        gap = later - earlier
        assert 420 <= gap <= 420 + cota_campaign.TICK_SECONDS + 2, gap


def test_the_device_list_carries_the_next_action(fleet, monkeypatch):
    from ota_analytics import cota_campaign as cc

    cid = cc.create(fleet.conn, name="t", device_ids=[786], commands_text=GET_FTP)
    rows, _ = cc.device_rows(fleet.conn, cid)
    assert rows[0]["next"]["what"] == "Sends" and rows[0]["next"]["left"] == 0
    assert len(rows[0]["next"]["clock"]) == 8 and "pending_task_id" not in rows[0]



# ── no job runs past its time limit (2.0.1) ───────────────────────────────

def test_a_job_stops_at_its_time_limit_whatever_is_left(fleet):
    job, cloud = fleet([786], text=FIVE, how=lambda d, v, n: "pending", time_limit_minutes=5)
    results = _results(fleet.conn, job["id"])
    states = [results[(786, s)]["state"] for s in range(5)]
    assert job["state"] == "done" and states[:3] == ["failed", "failed", "failed"]
    assert states[3] == "expired" and results[(786, 3)]["outcome"] == "time_limit"
    assert states[4] == "skipped"
    assert 300 <= fleet.clock.t <= 300 + 2 * cota_campaign.TICK_SECONDS


def test_two_devices_four_commands_finish_well_inside_an_hour_even_if_nothing_answers(fleet):
    """The case that prompted the rule: 2 devices × 4 commands took 12 hours on 07-10-2026."""
    four = "\n".join([GET_FTP, GET_6C0A, CLR_SOS, SET_6C0A])
    job, cloud = fleet([14906, 786], text=four, how=lambda d, v, n: "pending")
    assert job["state"] == "done" and job["results"] == {"failed": 8}
    sends = [t for t, _, _ in cloud.send_calls]
    assert all(29 <= b - a <= 31 for a, b in zip(sends, sends[1:]))  # 30 s apart, every one
    assert fleet.clock.t <= 4 * 90 + 20                              # ~90 s per command


@pytest.mark.parametrize("devices, steps, rate, over", [(2, 4, 5.0, False), (30000, 5, 5.0, True)])
def test_the_plan_states_the_worst_case_and_whether_it_fits_the_limit(fleet, devices, steps, rate, over):
    text = "\n".join([GET_FTP] * steps)
    p = cota_campaign.plan(fleet.conn, list(range(1, devices + 1)), text, rate_per_sec=rate)
    assert p["limit_minutes"] == 60 and p["over_limit"] is over and p["answer_wait"] == 30
    if devices == 2:
        assert p["worst_minutes"] == 8                               # 4 × 3 × (30 s + a tick)


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
