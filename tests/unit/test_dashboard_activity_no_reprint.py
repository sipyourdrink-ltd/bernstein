"""The ACTIVITY pane writes a log line once, not once per poll tick (#6139).

`_update_activity` de-duplicated on the FORMATTED line, which starts with the
poll time. So the same agent log tail differed on every tick and was written
again with a marching timestamp until it left the buffer: one `POST /tasks`
rendered as ten, which reads as an agent stuck in a loop.
"""

from __future__ import annotations

from typing import Any

import pytest

from bernstein.cli import dashboard_app, dashboard_polling
from bernstein.cli.dashboard_app import BernsteinApp


class _FakeLog:
    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, line: str) -> None:
        self.written.append(line)


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> tuple[BernsteinApp, _FakeLog, dict[str, list[str]]]:
    monkeypatch.delenv("BERNSTEIN_ACTIVITY_LOG", raising=False)
    instance = BernsteinApp()
    log = _FakeLog()
    monkeypatch.setattr(instance, "query_one", lambda *_a, **_k: log)
    tails: dict[str, list[str]] = {}
    monkeypatch.setattr(dashboard_app, "_tail_log", lambda aid, _n, log_path="": list(tails.get(aid, [])))
    # A new second on every tick, which is what made each formatted line unique.
    clock = iter(f"02:41:{s:02d}" for s in range(37, 99))
    monkeypatch.setattr(dashboard_polling.time, "strftime", lambda _fmt: next(clock))
    return instance, log, tails


AGENTS: list[dict[str, Any]] = [
    {"id": "m1", "role": "manager", "status": "working"},
    {"id": "f1", "role": "frontend", "status": "working"},
]


def test_an_unchanged_tail_is_not_written_again_on_the_next_tick(app: Any) -> None:
    instance, log, tails = app
    tails["m1"] = ["[Bash] curl -X POST /tasks", "Task A id captured"]
    tails["f1"] = ["Now let me read the test file."]

    for _ in range(4):
        instance._update_activity(AGENTS)

    assert len(log.written) == 3, log.written


def test_a_new_line_in_one_agents_tail_is_written_once(app: Any) -> None:
    instance, log, tails = app
    tails["m1"] = ["[Bash] curl -X POST /tasks", "Task A id captured"]
    instance._update_activity(AGENTS)
    tails["m1"] = ["Task A id captured", "Creating Task B"]

    instance._update_activity(AGENTS)
    instance._update_activity(AGENTS)

    assert [line for line in log.written if "Creating Task B" in line] != []
    assert sum("Creating Task B" in line for line in log.written) == 1
    assert sum("Task A id captured" in line for line in log.written) == 1


def test_the_same_text_from_two_agents_is_two_lines(app: Any) -> None:
    """Keyed per agent: two workers printing the same sentence both did something."""
    instance, log, tails = app
    tails["m1"] = ["Running tests"]
    tails["f1"] = ["Running tests"]

    instance._update_activity(AGENTS)

    assert sum("Running tests" in line for line in log.written) == 2
