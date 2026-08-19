"""What a long job is doing, while it does it.

A bundle merge takes minutes and an API fetch takes tens of seconds. Both used to run with the
browser holding an open POST and nothing to show for it, so the only feedback was a page that
would not finish loading — indistinguishable from a hang. That is exactly how a working merge
came to be reported as "no action, only loading".

Three decisions shape this module:

**The bar is determinate, and it is honest.** Every step declares a total before it starts, and
the fraction comes from work actually completed — snapshots folded, rows read — never from
elapsed time. A bar that advances on a timer is worse than no bar, because it teaches people to
ignore it precisely when it matters.

**Progress lives on the server, not in the browser tab.** The job keeps running if the page is
closed, and reopening the page finds it again. A spinner drawn in JavaScript cannot do that, and
with a two-minute merge someone will close the tab.

**One job at a time.** Every one of these writes to the database, so two at once would queue on
the write lock anyway — but silently, with two bars both claiming to move. A second start is
refused while one is running.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime

# Held in memory rather than in the database on purpose: this describes a job in *this* process.
# A row left behind by a process that has since been killed would be reported for ever as still
# running, which is the opposite of what a progress indicator is for.
_lock = threading.RLock()
_current: Job | None = None


@dataclass
class Step:
    """One named phase, carrying the denominator its share of the bar is drawn from."""

    name: str
    weight: float = 1.0      # how much of the overall bar this phase is worth
    total: int = 0           # 0 = no count available; the phase then only moves at its edges
    done: int = 0
    detail: str = ""

    @property
    def fraction(self) -> float:
        return min(1.0, self.done / self.total) if self.total > 0 else 0.0


@dataclass
class Job:
    kind: str                            # import | fetch | upload
    label: str
    steps: list[Step]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    index: int = 0
    status: str = "running"              # running | done | error
    message: str = ""
    started_at: str = field(
        default_factory=lambda: datetime.now().isoformat(sep=" ", timespec="seconds"))
    finished_at: str | None = None
    error: str = ""

    # ── writing ────────────────────────────────────────────────────────────

    def begin(self, name: str, total: int = 0, detail: str = "") -> None:
        """Move to the named step, appending it if the job discovered work it had not declared."""
        with _lock:
            for position, step in enumerate(self.steps):
                if step.name == name:
                    self.index = position
                    break
            else:
                self.steps.append(Step(name=name))
                self.index = len(self.steps) - 1

            step = self.steps[self.index]
            step.total, step.done, step.detail = total, 0, detail
            # Anything before the current step is finished by definition. Without this the bar
            # would go backwards whenever a phase turns out to have nothing to do.
            for earlier in self.steps[:self.index]:
                earlier.done = earlier.total or earlier.done or 1
                earlier.total = earlier.total or earlier.done

    def advance(self, done: int | None = None, detail: str | None = None) -> None:
        with _lock:
            step = self.steps[self.index]
            step.done = step.done + 1 if done is None else done
            if detail is not None:
                step.detail = detail

    def finish(self, message: str) -> None:
        with _lock:
            self.index = max(0, len(self.steps) - 1)
            for step in self.steps:
                step.done = step.total or step.done or 1
                step.total = step.total or step.done
            self.status, self.message = "done", message
            self.finished_at = datetime.now().isoformat(sep=" ", timespec="seconds")

    def fail(self, exc: BaseException) -> None:
        """Stop where it stopped. The bar keeps its position so the failed phase is visible."""
        with _lock:
            self.status = "error"
            self.message = f"{type(exc).__name__}: {exc}"
            self.error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self.finished_at = datetime.now().isoformat(sep=" ", timespec="seconds")

    # ── reading ────────────────────────────────────────────────────────────

    @property
    def fraction(self) -> float:
        """Overall completion, weighted by step and derived from work done."""
        with _lock:
            total_weight = sum(s.weight for s in self.steps) or 1.0
            earned = 0.0
            for position, step in enumerate(self.steps):
                if position < self.index:
                    earned += step.weight
                elif position == self.index:
                    earned += step.weight * step.fraction
            return max(0.0, min(1.0, earned / total_weight))

    def to_dict(self) -> dict:
        with _lock:
            step = self.steps[self.index] if self.steps else Step("")
            return {
                "id": self.id,
                "kind": self.kind,
                "label": self.label,
                "status": self.status,
                "percent": round(self.fraction * 100, 1),
                "step": step.name,
                "step_index": self.index + 1,
                "step_count": len(self.steps),
                "done": step.done,
                "total": step.total,
                "detail": step.detail,
                "message": self.message,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


class Busy(Exception):
    """Raised when a job is requested while another is still running."""


def start(kind: str, label: str, steps: list[tuple[str, float]]) -> Job:
    """Begin a job, refusing if one is already running.

    `steps` is (name, weight). Weights come from measurement rather than guesswork — replaying
    the change log is most of a merge, so it is worth most of the bar. Even weights would leave
    the bar apparently stalled for a minute at the point people are most likely to give up.
    """
    global _current
    with _lock:
        if _current is not None and _current.status == "running":
            raise Busy(f"{_current.label} is still running.")
        _current = Job(kind=kind, label=label,
                       steps=[Step(name=name, weight=weight) for name, weight in steps])
        return _current


def current() -> Job | None:
    with _lock:
        return _current


def clear() -> None:
    """Forget the last job, once its result has been shown."""
    global _current
    with _lock:
        _current = None


def snapshot() -> dict:
    """What the page polls. Always answers, so the poller needs no special cases."""
    job = current()
    return {"active": False} if job is None else {"active": True, **job.to_dict()}
