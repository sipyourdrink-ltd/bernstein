"""Score the auditor conformance suite: how many of the 21 are answered.

Registered as a pytest plugin by ``scripts/auditor_conformance.py``. It
records the outcome of every test carrying ``@pytest.mark.question(n)``
and writes the result to the file named by
``BERNSTEIN_AUDITOR_SCORE_JSON``.

The numerator is what the run produced. A vector that fails - or errors,
or is skipped for want of an environment - is not counted, so the score
degrades when the evidence does instead of quietly staying put.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.auditor.questions import TOTAL_QUESTIONS

#: Environment variable naming where the score report is written.
REPORT_PATH_ENV = "BERNSTEIN_AUDITOR_SCORE_JSON"


@dataclass(frozen=True, slots=True)
class Score:
    """A scored run of the vectors.

    Attributes:
        passed: Question numbers whose vector passed, ascending.
        failed: Question numbers whose vector ran and did not pass.
        total: The full question set - never the number of vectors.
    """

    passed: tuple[int, ...]
    failed: tuple[int, ...]
    total: int

    def __str__(self) -> str:
        return f"{len(self.passed)}/{self.total}"


def _question_number(item: pytest.Item) -> int | None:
    marker = item.get_closest_marker("question")
    if marker is None or not marker.args:
        return None
    return int(marker.args[0])


class ScorePlugin:
    """Collects question outcomes and writes the report at session end."""

    def __init__(self, report_path: Path | None) -> None:
        self._report_path = report_path
        self._numbers: dict[str, int] = {}
        self._outcomes: dict[int, bool] = {}

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        """Remember which question each collected vector answers."""
        for item in items:
            number = _question_number(item)
            if number is not None:
                self._numbers[item.nodeid] = number

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Record a vector's outcome; any non-pass counts as unanswered."""
        number = self._numbers.get(report.nodeid)
        if number is None:
            return
        if report.when == "call" and report.passed:
            self._outcomes.setdefault(number, True)
        elif report.failed or report.skipped:
            self._outcomes[number] = False

    def score(self) -> Score:
        """Return the score this session produced."""
        passed = tuple(sorted(n for n, ok in self._outcomes.items() if ok))
        failed = tuple(sorted(n for n, ok in self._outcomes.items() if not ok))
        return Score(passed=passed, failed=failed, total=TOTAL_QUESTIONS)

    def pytest_sessionfinish(self) -> None:
        """Write the report when a destination was configured."""
        if self._report_path is None:
            return
        write_report(self._report_path, self.score())


#: Name the collecting plugin registers under, so a double ``-p`` is a no-op.
PLUGIN_NAME = "auditor-conformance-scoreboard"


def pytest_configure(config: pytest.Config) -> None:
    """Register the collector when this module is loaded with ``-p``."""
    if config.pluginmanager.has_plugin(PLUGIN_NAME):
        return
    config.pluginmanager.register(ScorePlugin(report_path_from_env()), PLUGIN_NAME)


def write_report(path: Path, score: Score) -> None:
    """Write *score* to *path* as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "passed": list(score.passed),
        "failed": list(score.failed),
        "total": score.total,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_report(path: Path) -> Score:
    """Read a score report written by :func:`write_report`.

    Args:
        path: The report file.

    Returns:
        The recorded :class:`Score`, with the denominator taken from the
        live registry so a stale report cannot understate the question set.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Score(
        passed=tuple(sorted(int(n) for n in payload.get("passed", []))),
        failed=tuple(sorted(int(n) for n in payload.get("failed", []))),
        total=TOTAL_QUESTIONS,
    )


def report_path_from_env() -> Path | None:
    """Return the configured report destination, or ``None``."""
    raw = os.environ.get(REPORT_PATH_ENV)
    return Path(raw) if raw else None


__all__ = [
    "PLUGIN_NAME",
    "REPORT_PATH_ENV",
    "Score",
    "ScorePlugin",
    "read_report",
    "report_path_from_env",
    "write_report",
]
