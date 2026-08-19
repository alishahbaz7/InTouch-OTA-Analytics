"""The progress bar, and the two properties that make it worth having.

It has to be **honest** — the width comes from work completed, never from elapsed time — and it
has to live **on the server**, so closing the tab does not stop the job and reopening the page
finds it again. A bar that fails either is worse than none: the first teaches people to distrust
it, and the second sends them to kill a process that was working.
"""

from __future__ import annotations

import time

import pytest

from ota_analytics import progress


@pytest.fixture(autouse=True)
def clean():
    progress.clear()
    yield
    progress.clear()


STEPS = [("Reading", 1.0), ("Loading", 4.0), ("Rebuilding", 5.0)]


# ─── the bar tracks work, not time ──────────────────────────────────────────

def test_the_bar_does_not_move_on_its_own():
    """The whole reason to prefer determinate: elapsed time must not advance it."""
    job = progress.start("import", "Merging", STEPS)
    job.begin("Loading", total=100)
    job.advance(25)

    before = progress.snapshot()["percent"]
    time.sleep(0.2)
    assert progress.snapshot()["percent"] == before

    job.advance(50)
    assert progress.snapshot()["percent"] > before


def test_steps_are_weighted_by_what_they_actually_cost():
    """Even weights would leave the bar stalled through the phase that takes 60% of a merge."""
    job = progress.start("import", "Merging", STEPS)
    job.begin("Reading")
    job.begin("Loading", total=4)
    job.advance(4)
    # Reading (1) and Loading (4) complete out of 10 total weight.
    assert progress.snapshot()["percent"] == pytest.approx(50.0)

    job.begin("Rebuilding", total=10)
    job.advance(5)
    assert progress.snapshot()["percent"] == pytest.approx(75.0)


def test_the_bar_never_goes_backwards_when_a_step_is_skipped():
    """A merge skips phases it does not need, and a bar that jumps back reads as a fault."""
    job = progress.start("import", "Merging", STEPS)
    job.begin("Reading")
    job.begin("Rebuilding", total=10)     # Loading skipped entirely
    job.advance(1)
    assert progress.snapshot()["percent"] >= 50.0


def test_a_step_with_no_count_still_advances_the_bar_at_its_edges():
    """Some phases cannot report a total. They must not stall the bar at 0 for their duration."""
    job = progress.start("import", "Merging", STEPS)
    job.begin("Reading")
    assert progress.snapshot()["percent"] == 0.0
    job.begin("Loading")                   # Reading is over, so its weight is earned
    assert progress.snapshot()["percent"] == pytest.approx(10.0)


def test_finishing_fills_the_bar_completely():
    job = progress.start("import", "Merging", STEPS)
    job.begin("Loading", total=100)
    job.advance(3)
    job.finish("37 snapshots merged")

    state = progress.snapshot()
    assert state["percent"] == 100.0
    assert state["status"] == "done"
    assert state["message"] == "37 snapshots merged"


def test_a_failure_stops_where_it_stopped():
    """The bar keeps its position so the phase that failed is still identifiable."""
    job = progress.start("import", "Merging", STEPS)
    job.begin("Loading", total=100)
    job.advance(40)
    midway = progress.snapshot()["percent"]

    job.fail(ValueError("bundle is damaged"))
    state = progress.snapshot()

    assert state["status"] == "error"
    assert state["percent"] == pytest.approx(midway)
    assert "damaged" in state["message"]
    assert state["step"] == "Loading"


# ─── one job at a time ──────────────────────────────────────────────────────

def test_a_second_job_is_refused_while_one_runs():
    """Two would queue on the write lock anyway — but silently, with two bars both moving."""
    progress.start("import", "Merging bundle", STEPS)
    with pytest.raises(progress.Busy, match="Merging bundle"):
        progress.start("fetch", "Fetching", STEPS)


def test_a_finished_job_does_not_block_the_next_one():
    progress.start("import", "Merging", STEPS).finish("done")
    progress.start("fetch", "Fetching", STEPS)      # must not raise
    assert progress.snapshot()["label"] == "Fetching"


def test_a_failed_job_does_not_block_the_next_one():
    progress.start("import", "Merging", STEPS).fail(RuntimeError("nope"))
    progress.start("fetch", "Fetching", STEPS)
    assert progress.snapshot()["label"] == "Fetching"


# ─── it survives the page, because it is not in the page ────────────────────

def test_progress_is_readable_from_anywhere_not_just_the_tab_that_started_it():
    """A job outlives the request that started it, so any later reader can find it."""
    job = progress.start("import", "Merging", STEPS)
    job.begin("Loading", total=37)
    job.advance(12, detail="snapshot 12 of 37")

    seen = progress.snapshot()
    assert seen["active"] is True
    assert seen["done"] == 12 and seen["total"] == 37
    assert seen["detail"] == "snapshot 12 of 37"
    assert seen["step_index"] == 2 and seen["step_count"] == 3


def test_nothing_running_answers_plainly():
    """The poller has no special cases, so this must always be a valid answer."""
    assert progress.snapshot() == {"active": False}


def test_a_job_discovering_extra_work_appends_rather_than_misreporting():
    job = progress.start("import", "Merging", STEPS)
    job.begin("Something unplanned", total=2)
    state = progress.snapshot()
    assert state["step"] == "Something unplanned"
    assert state["step_count"] == 4


# ─── wired into the real operations ─────────────────────────────────────────

def test_the_merge_declares_phases_that_add_up():
    from ota_analytics import bundle

    names = [name for name, _ in bundle.MERGE_STEPS]
    assert "Replaying the change log" in names
    # It is 60% of a real merge, so it must be weighted as such or the bar stalls there.
    weights = dict(bundle.MERGE_STEPS)
    assert weights["Replaying the change log"] == max(weights.values())


def test_ingest_and_fetch_declare_phases_too():
    from ota_analytics import ingest, scheduler

    assert [n for n, _ in ingest.INGEST_STEPS][0] == "Saving the file"
    assert "Downloading from the platform" in [n for n, _ in scheduler.FETCH_STEPS]
    for steps in (ingest.INGEST_STEPS, scheduler.FETCH_STEPS):
        assert all(weight > 0 for _, weight in steps)
