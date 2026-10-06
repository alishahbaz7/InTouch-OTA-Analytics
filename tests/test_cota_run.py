"""The sequence runner against a simulated device and cloud.

Every failure case the user listed (2026-10-06) is acted out here — device asleep, API not
working, device missed the reply, device never received it, wrong command — plus a late answer,
an expired session mid-run, pause, resume and cancel. Time is simulated: a run that would take
twenty minutes on the desk device takes milliseconds, and every wait is still the real length.

The five commands are the user's own test list.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from ota_analytics import cota, cota_run, db

UNKNOWN = "DCD76D66"                      # not a known operation byte
GET_FTP, GET_6C0A, ACTION, SET_6C0A, SET_6B38 = (
    "DAD76F4B", "DAD76C0A", "DDD76D66",
    "DBD76C0AD531D931D9322E35D9332E35D9332E35D931D93130D933",
    "DBD76B38D530303030303030303030D930303030303030303030D930303030303030303030"
    "D930303030303030303030D930303030303030303030")
USER_LIST = "\n".join(f"{n}. {c}" for n, c in
                      enumerate([GET_FTP, GET_6C0A, ACTION, SET_6C0A, SET_6B38], start=1))
BASE = datetime(2026, 10, 6, 12, 0, 0)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.hooks = []                  # (at, fn): run once when time passes `at`

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        for hook in list(self.hooks):
            if self.t >= hook[0]:
                self.hooks.remove(hook)
                hook[1]()

    def wall(self):
        return BASE + timedelta(seconds=self.t)


class Device:
    """The cloud and the device behind it. `script[value]` lists, per send of that value, how
    the attempt goes; anything unscripted is answered after 20 s."""

    def __init__(self, clock, script=None):
        self.clock, self.script = clock, {k: list(v) for k, v in (script or {}).items()}
        self.records, self.sends, self.n = [], [], 0
        self.reject_sends = 0            # next N sends answer 401
        self.reject_checks = 0           # next N checks answer 401
        self.fail_checks = 0             # next N checks answer 503

    def send(self, payload):
        value = payload["val1"]
        self.sends.append((self.clock.t, value))
        if self.reject_sends:
            self.reject_sends -= 1
            raise cota.CotaError("The cloud rejected the token (HTTP 401). It has most likely "
                                 "expired — sign in again with a fresh token from the portal.")
        how = self.script.get(value, []).pop(0) if self.script.get(value) else "answer:20"
        kind, _, arg = how.partition(":")
        if kind == "api_down":
            raise cota.CotaError("Could not reach the COTA cloud: ConnectError.")
        if kind == "http500":
            return 500, '{"error": "internal"}'
        self.n += 1
        if kind != "lost":
            self.records.append({"id": f"865510083360422_{self.n:08X}", "deviceId": 14906,
                                 "timestamp": int(self.clock.wall().timestamp()),
                                 "type": payload["type"], "val1": "245A" + value, "status": 0,
                                 "response": None, "responseTime": None,
                                 "imei": "865510083360422", "_how": kind,
                                 "_after": float(arg or 0), "_t0": self.clock.t})
        return 200, '{"msg":"Command send Successfully.."}'

    def responses(self, device_id, start, end):
        if self.reject_checks:
            self.reject_checks -= 1
            raise cota.CotaError("The cloud rejected the token (HTTP 401). It has most likely "
                                 "expired — sign in again with a fresh token from the portal.")
        if self.fail_checks:
            self.fail_checks -= 1
            return 503, "busy"
        out = []
        for r in self.records:
            rec = {k: v for k, v in r.items() if not k.startswith("_")}
            age = self.clock.t - r["_t0"]
            if age >= r["_after"]:
                if r["_how"] == "answer":
                    rec.update(status=1, response=f"(OK {r['val1'][4:12]})*EF")
                elif r["_how"] == "fail":
                    rec.update(status=1, response="SET TIMERS:KEEP,FAIL,KEEP FAILED#123")
                elif r["_how"] == "note":
                    rec.update(status=1, response="Command sent at 2026-10-06 12:00:05")
            out.append(rec)
        return 200, json.dumps({"data": out})

    def close(self):
        pass

    def sent_values(self):
        return [v for _, v in self.sends]


@pytest.fixture
def sim(monkeypatch, tmp_path):
    clock = Clock()
    monkeypatch.setattr(cota, "_now", lambda: clock.wall().strftime(cota.TIME_FORMAT))
    # No real background runs from a test: a started run is recorded, never executed. And a
    # fresh thread registry, so one test's runs cannot look alive in another's.
    started = []
    monkeypatch.setattr(cota_run, "start", lambda run_id: started.append(run_id))
    monkeypatch.setattr(cota_run, "_threads", {})
    conn = db.connect()

    def run(text, script=None, renew=lambda: False, device=None, run_id=None):
        device = device or Device(clock, script)
        rid = run_id or cota_run.create_run(conn, 14906, 124, 36, text)
        cota_run.Runner(rid, client_factory=lambda: device, renew=renew, sleep=clock.sleep,
                        clock=clock.now).run()
        return cota_run.run_view(conn, rid), device

    run.clock, run.conn, run.started = clock, conn, started
    return run


def _attempts(view):
    return [s["attempts"] for s in view["steps"]]


def _outcomes(step):
    return [e["outcome"] for e in step["log"]]


# ── reading the commands ──────────────────────────────────────────────────

def test_the_users_list_reads_as_two_gets_a_clear_and_two_sets():
    steps = cota_run.parse_lines(USER_LIST)
    assert [s["kind"] for s in steps] == ["get", "get", "clear", "set", "set"]
    assert [cota.describe_command(s["val1"]) for s in steps] == [
        "GET FTP_SETTINGS", "GET 6C0A", "CLR SOS", "SET 6C0A", "SET 6B38"]
    assert [s["val1"] for s in steps] == [GET_FTP, GET_6C0A, ACTION, SET_6C0A, SET_6B38]
    assert not any(s["problem"] for s in steps)


@pytest.mark.parametrize("value, problem", [
    ("DAD76F4", "odd number"), ("XYZ12345", "not hexadecimal"), ("DA6F4B00", "does not look like"),
    ("DA", "does not look like")])
def test_a_wrong_command_is_caught_before_anything_is_sent(sim, value, problem):
    with pytest.raises(cota_run.RunError) as caught:
        cota_run.create_run(sim.conn, 14906, 124, 36, f"{GET_FTP}\n{value}")
    assert "line 2" in str(caught.value) and problem in str(caught.value)
    assert sim.conn.execute("SELECT COUNT(*) FROM cota_run").fetchone()[0] == 0


# ── the happy path, and the agreed shape of a run ─────────────────────────

def test_all_five_go_one_at_a_time_in_order_with_the_gaps(sim):
    view, device = sim(USER_LIST)
    assert view["state"] == "done" and view["succeeded"] == 5 and _attempts(view) == [1] * 5
    assert device.sent_values() == [GET_FTP, GET_6C0A, ACTION, SET_6C0A, SET_6B38]
    times = [t for t, _ in device.sends]
    gaps = [b - a for a, b in zip(times, times[1:])]
    # Each answered at 20 s, seen on that 10 s check, and the next one 2 s later.
    assert gaps == [20 + cota_run.GAP_AFTER_ANSWER_SECONDS] * 4
    # never two in flight: each step ended before the next began
    for earlier, later in zip(view["steps"], view["steps"][1:]):
        assert earlier["finished_at"] <= later["started_at"]


# ── the failure cases ─────────────────────────────────────────────────────

def test_api_not_working_is_retried_and_the_command_still_goes(sim):
    view, device = sim(GET_FTP, {GET_FTP: ["api_down", "http500", "answer:20"]})
    step = view["steps"][0]
    assert step["state"] == "done" and step["attempts"] == 1      # refused calls are not attempts
    assert _outcomes(step) == ["not_accepted", "not_accepted", "answered"]


def test_the_api_down_for_three_calls_pauses_the_run(sim):
    view, _ = sim(f"{GET_FTP}\n{GET_6C0A}", {GET_FTP: ["api_down", "api_down", "api_down"]})
    assert view["state"] == "paused" and "did not accept 3 calls" in view["pause_reason"]
    assert view["steps"][1]["state"] == "queued"     # nothing skipped while the API is down


def test_a_command_the_cloud_never_listed_is_resent(sim):
    view, device = sim(ACTION, {ACTION: ["lost", "answer:20"]})
    step = view["steps"][0]
    assert step["state"] == "done" and _outcomes(step) == ["not_in_cloud", "answered"]
    assert device.sent_values() == [ACTION, ACTION]  # even CLR SOS: it provably never arrived


def test_a_sleeping_device_gets_three_tries_then_the_run_moves_on(sim):
    view, device = sim(f"{GET_FTP}\n{GET_6C0A}", {GET_FTP: ["pending", "pending", "pending"]})
    first, second = view["steps"]
    assert first["state"] == "failed" and _outcomes(first) == ["not_delivered"] * 3
    assert second["state"] == "done"                 # "retry 3 attempts, then jump to next"
    assert view["state"] == "done"


def test_an_unknown_command_the_device_may_have_received_is_never_sent_twice(sim):
    view, device = sim(f"{UNKNOWN}\n{GET_FTP}", {UNKNOWN: ["pending"]})
    step = view["steps"][0]
    assert step["state"] == "failed" and step["attempts"] == 1
    assert device.sent_values().count(UNKNOWN) == 1
    assert view["steps"][1]["state"] == "done"


def test_clr_sos_is_retried_like_any_normal_command(sim):
    """The user's decision: CLR goes again like GET and SET."""
    view, device = sim(f"{ACTION}\n{GET_FTP}", {ACTION: ["pending", "note:15", "answer:20"]})
    step = view["steps"][0]
    assert step["state"] == "done" and step["attempts"] == 3
    assert _outcomes(step) == ["not_delivered", "delivered_no_answer", "answered"]


def test_delivered_but_unanswered_is_retried_for_a_get(sim):
    view, _ = sim(GET_6C0A, {GET_6C0A: ["note:15", "note:15", "answer:20"]})
    step = view["steps"][0]
    assert step["state"] == "done"
    assert _outcomes(step) == ["delivered_no_answer", "delivered_no_answer", "answered"]


def test_a_failed_answer_is_retried_three_times_then_the_run_moves_on(sim):
    view, device = sim(f"{SET_6C0A}\n{GET_6C0A}", {SET_6C0A: ["fail:20", "fail:20", "fail:20"]})
    first, second = view["steps"]
    assert first["state"] == "failed" and _outcomes(first) == ["answered_failure"] * 3
    assert first["outcome"] == "answered_failure" and second["state"] == "done"


def test_a_failed_clear_is_retried_three_times(sim):
    view, device = sim(ACTION, {ACTION: ["fail:20", "fail:20", "fail:20"]})
    assert view["steps"][0]["attempts"] == 3 and device.sent_values() == [ACTION] * 3


def test_a_failed_unknown_command_is_not_repeated(sim):
    view, device = sim(UNKNOWN, {UNKNOWN: ["fail:20"]})
    assert view["steps"][0]["attempts"] == 1 and device.sent_values() == [UNKNOWN]


def test_a_late_answer_to_an_earlier_attempt_still_counts(sim):
    # The first attempt answers at 150 s — after its 120 s wait. The resend is out by then; the
    # late answer arrives while it is being watched, and the step is done without a third send.
    view, device = sim(GET_FTP, {GET_FTP: ["answer:150", "pending"]})
    step = view["steps"][0]
    assert step["state"] == "done" and step["attempts"] == 2
    assert device.sent_values() == [GET_FTP, GET_FTP]


# ── sessions, pausing, resuming, cancelling ───────────────────────────────

def test_an_expired_session_mid_run_renews_and_carries_on(sim):
    device = Device(sim.clock)
    # step 1 is answered at 20 s; step 2 goes 2 s later — expire the session in between
    sim.clock.hooks.append((21, lambda: setattr(device, "reject_sends", 1)))
    renewed = []
    view, _ = sim(f"{GET_FTP}\n{GET_6C0A}", device=device, renew=lambda: renewed.append(1) or True)
    assert view["state"] == "done" and renewed == [1]
    assert device.sent_values().count(GET_6C0A) == 2    # the refused one, then the real one


def test_an_expired_session_without_a_saved_password_pauses_and_resumes(sim):
    device = Device(sim.clock)
    # step 1 is answered at 20 s; step 2 goes 2 s later — expire the session in between
    sim.clock.hooks.append((21, lambda: setattr(device, "reject_sends", 1)))
    view, _ = sim(f"{GET_FTP}\n{GET_6C0A}", device=device)
    assert view["state"] == "paused" and "Session expired" in view["pause_reason"]
    assert view["steps"][0]["state"] == "done" and view["steps"][1]["state"] == "running"

    cota_run.request(sim.conn, view["id"], "resume")
    assert sim.started == [view["id"]]                   # resuming asks for a run…
    view, _ = sim("", device=device, run_id=view["id"])  # …which the test then performs
    assert view["state"] == "done"
    assert device.sent_values().count(GET_FTP) == 1      # step 1 was not sent again


def test_a_cloud_that_stops_answering_checks_pauses_and_resumes_on_the_same_attempt(sim):
    device = Device(sim.clock, {GET_FTP: ["answer:60"]})
    sim.clock.hooks.append((15, lambda: setattr(device, "fail_checks", 3)))
    view, _ = sim(GET_FTP, device=device)
    assert view["state"] == "paused" and "did not answer 3 checks" in view["pause_reason"]
    assert view["steps"][0]["pending_task_id"]           # sent; its outcome not yet decided

    with sim.conn:
        sim.conn.execute("UPDATE cota_run SET state = 'running' WHERE id = ?", (view["id"],))
    view, _ = sim("", device=device, run_id=view["id"])
    assert view["state"] == "done" and view["steps"][0]["attempts"] == 1
    assert device.sent_values() == [GET_FTP]             # watched again, not resent


def test_cancel_stops_the_run_and_cancels_what_is_left(sim):
    run_id = cota_run.create_run(sim.conn, 14906, 124, 36, f"{GET_FTP}\n{GET_6C0A}\n{ACTION}")
    # Step 1 is answered at 20 s; the cancel comes in the 2 s before step 2 would go.
    sim.clock.hooks.append((21, lambda: cota_run.request(sim.conn, run_id, "cancel")))
    view, device = sim("", run_id=run_id)
    assert view["state"] == "cancelled"
    assert [s["state"] for s in view["steps"]] == ["done", "cancelled", "cancelled"]
    assert device.sent_values() == [GET_FTP]


def test_only_one_sequence_per_device_at_a_time(sim):
    cota_run.create_run(sim.conn, 14906, 124, 36, GET_FTP)
    with pytest.raises(cota_run.RunError, match="already running"):
        cota_run.create_run(sim.conn, 14906, 124, 36, GET_6C0A)


def test_a_run_interrupted_by_a_restart_comes_back_paused(sim):
    run_id = cota_run.create_run(sim.conn, 14906, 124, 36, GET_FTP)
    assert cota_run.recover(sim.conn) == 1
    view = cota_run.run_view(sim.conn, run_id)
    assert view["state"] == "paused" and "restarted" in view["pause_reason"]


def test_every_attempt_shows_in_the_conversation(sim):
    view, _ = sim(f"{GET_FTP}\n{GET_6C0A}", {GET_FTP: ["lost", "answer:20"]})
    th = cota.thread(sim.conn, 14906)
    assert [c["params"]["val1"] for c in th["entries"]] == [GET_FTP, GET_FTP, GET_6C0A]
    assert [c["stage"] for c in th["entries"]][-1] == "device"


def test_a_runner_that_hits_an_unexpected_error_pauses_its_run_and_logs_it(sim):
    class Broken(Device):
        def send(self, payload):
            raise KeyError("something nobody planned for")

    view, _ = sim(GET_FTP, device=Broken(sim.clock))
    assert view["state"] == "paused" and "unexpected error (KeyError)" in view["pause_reason"]
    from ota_analytics import errors
    assert any(e["source"] == "cota-run" for e in errors.recent(sim.conn))


def test_the_guard_band_comes_before_a_resend_and_after_a_command_that_gave_up(sim):
    view, device = sim(f"{GET_FTP}\n{GET_6C0A}", {GET_FTP: ["pending", "pending", "pending"]})
    times = [t for t, _ in device.sends]
    # Three attempts of GET_FTP, each waiting its 2 minutes (checked every 10 s), then the guard.
    first_to_second = times[1] - times[0]
    assert first_to_second == cota_run.ANSWER_WAIT_SECONDS + cota_run.GUARD_SECONDS["get"]
    assert times[2] - times[1] == first_to_second
    # It gave up: the guard band again before the next command, not the 2 s after an answer.
    assert times[3] - times[2] == cota_run.ANSWER_WAIT_SECONDS + cota_run.GUARD_SECONDS["get"]


def test_a_refused_send_waits_the_guard_band_before_trying_again(sim):
    view, device = sim(GET_FTP, {GET_FTP: ["api_down", "answer:20"]})
    times = [t for t, _ in device.sends]
    assert times[1] - times[0] == cota_run.GUARD_SECONDS["get"]


@pytest.mark.parametrize("value, guard", [(ACTION, 30), (UNKNOWN, 60)])
def test_clr_has_the_normal_guard_band_and_an_unknown_command_the_longer_one(sim, value, guard):
    view, device = sim(value, {value: ["lost", "answer:20"]})       # never listed: safe to resend
    times = [t for t, _ in device.sends]
    assert times[1] - times[0] == cota_run.NOT_IN_CLOUD_SECONDS + guard


def test_an_outage_does_not_use_up_a_commands_attempts(sim):
    """Three refusals pause the run; on resume the command still has all three attempts."""
    device = Device(sim.clock, {GET_FTP: ["api_down", "api_down", "api_down", "answer:20"]})
    view, _ = sim(GET_FTP, device=device)
    assert view["state"] == "paused" and view["steps"][0]["attempts"] == 0
    with sim.conn:
        sim.conn.execute("UPDATE cota_run SET state = 'running' WHERE id = ?", (view["id"],))
    view, _ = sim("", device=device, run_id=view["id"])
    assert view["state"] == "done" and view["steps"][0]["attempts"] == 1
