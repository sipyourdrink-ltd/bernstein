"""Fixtures and bookkeeping for the auditor conformance suite.

Three things live here:

* the fixture - one scenario run and one exported bundle per session,
  built by :mod:`tests.integration.conformance.auditor.scenario`;
* the isolated interpreter every verification vector shells out to,
  which has ``cryptography`` and ``cbor2`` and no ``bernstein``;
* the question bookkeeping that lets ``scripts/auditor_scoreboard.py``
  print ``n/21`` without parsing pytest's terminal output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.integration.conformance.auditor.bundle_reader import BundleReader
from tests.integration.conformance.auditor.isolation import create_isolated_interpreter
from tests.integration.conformance.auditor.questions import QUESTION_COUNT, QUESTIONS
from tests.integration.conformance.auditor.scenario import ScenarioFixture, build_fixture

#: Env var naming where to write the machine-readable scoreboard.
SCOREBOARD_ENV_VAR = "BERNSTEIN_AUDITOR_SCOREBOARD"

#: nodeid -> question number, filled at collection time.
_QUESTION_BY_NODEID: dict[str, int] = {}
#: question number -> "answered" | "unanswered", filled as tests report.
_OUTCOME_BY_QUESTION: dict[int, str] = {}


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def auditor_fixture(tmp_path_factory: pytest.TempPathFactory) -> ScenarioFixture:
    """Run the scenario once per session and export its bundle.

    The scenario is re-run rather than read from a checked-in copy on
    purpose: a committed bundle would keep answering questions about the
    code that produced it, not the code under test.
    """
    return build_fixture(tmp_path_factory.mktemp("auditor-fixture"))


@pytest.fixture(scope="session")
def auditor_bundle(auditor_fixture: ScenarioFixture) -> BundleReader:
    """The exported bundle, and the only thing a vector may read."""
    return BundleReader(auditor_fixture.bundle)


@pytest.fixture(scope="session")
def isolated_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An interpreter with the verifier's dependencies and no ``bernstein``."""
    return create_isolated_interpreter(tmp_path_factory.mktemp("auditor-verifier-venv"))


# ---------------------------------------------------------------------------
# Question bookkeeping
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Bind each vector to its question and refuse an unusable claim."""
    claimed: dict[int, str] = {}
    for item in items:
        marker = item.get_closest_marker("auditor_question")
        if marker is None:
            continue
        if len(marker.args) != 1 or not isinstance(marker.args[0], int):
            raise pytest.UsageError(
                f"{item.nodeid}: auditor_question takes exactly one question number",
            )
        number = marker.args[0]
        if number not in QUESTIONS:
            raise pytest.UsageError(
                f"{item.nodeid}: question {number} is not one of the {QUESTION_COUNT} registered "
                f"questions; add it to tests/integration/conformance/auditor/questions.py first",
            )
        if number in claimed:
            raise pytest.UsageError(
                f"{item.nodeid}: question {number} is already answered by {claimed[number]}",
            )
        claimed[number] = item.nodeid
        _QUESTION_BY_NODEID[item.nodeid] = number
        _OUTCOME_BY_QUESTION.setdefault(number, "unanswered")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record whether the vector for a question actually answered it."""
    number = _QUESTION_BY_NODEID.get(report.nodeid)
    if number is None:
        return
    if report.when == "call" and report.passed and not hasattr(report, "wasxfail"):
        _OUTCOME_BY_QUESTION[number] = "answered"
    elif report.failed:
        _OUTCOME_BY_QUESTION[number] = "unanswered"


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Write the machine-readable scoreboard when one was asked for."""
    target = os.environ.get(SCOREBOARD_ENV_VAR)
    if not target:
        return
    answered = sorted(n for n, outcome in _OUTCOME_BY_QUESTION.items() if outcome == "answered")
    payload = {
        "total": QUESTION_COUNT,
        "answered": answered,
        "asked": sorted(_OUTCOME_BY_QUESTION),
        "outcomes": {str(n): _OUTCOME_BY_QUESTION[n] for n in sorted(_OUTCOME_BY_QUESTION)},
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
