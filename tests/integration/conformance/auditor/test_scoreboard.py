"""The score has to be quotable, and it has to be honest.

``scripts/auditor_scoreboard.py`` turns the suite's report into one
line. These tests pin the two things that make the line worth quoting:
the denominator is the registered question count, and a question that
only has a strict ``xfail`` counts as unanswered.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from tests.integration.conformance.auditor.isolation import REPO_ROOT
from tests.integration.conformance.auditor.questions import QUESTION_COUNT

SCOREBOARD_SCRIPT = REPO_ROOT / "scripts" / "auditor_scoreboard.py"


@pytest.fixture(scope="module")
def scoreboard() -> ModuleType:
    """Load the scoreboard script as a module, the way the repo tests scripts."""
    spec = importlib.util.spec_from_file_location("auditor_scoreboard", SCOREBOARD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheScoreboardLine:
    """What ``n/21`` means."""

    def test_the_denominator_is_the_registered_question_count(self, scoreboard: ModuleType) -> None:
        """The suite and the scoreboard divide by the same number."""
        rendered = scoreboard.render(
            {"total": QUESTION_COUNT, "answered": [17], "outcomes": {"17": "answered"}},
        )
        assert f"answered 1/{QUESTION_COUNT}" in rendered

    def test_an_unanswered_question_does_not_count_towards_the_score(
        self,
        scoreboard: ModuleType,
    ) -> None:
        """An xfailed vector is asked, not answered."""
        rendered = scoreboard.render(
            {
                "total": QUESTION_COUNT,
                "answered": [17],
                "outcomes": {"3": "unanswered", "17": "answered"},
            },
        )
        assert f"asked 2/{QUESTION_COUNT}, answered 1/{QUESTION_COUNT}" in rendered
        assert "Q3   unanswered" in rendered
        assert "Q17  answered" in rendered

    def test_the_script_is_executable_as_a_repo_target(self) -> None:
        """The documented command exists where the docs say it does."""
        assert SCOREBOARD_SCRIPT.is_file()
        assert SCOREBOARD_SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3")


class TestTheSuiteReportsWhatTheScoreboardReads:
    """The report the suite writes is the report the script parses."""

    def test_the_script_and_the_suite_agree_on_the_report_channel(
        self,
        scoreboard: ModuleType,
    ) -> None:
        """Two spellings of the env var would silently render an empty score."""
        from tests.integration.conformance.auditor.conftest import SCOREBOARD_ENV_VAR

        assert scoreboard.SCOREBOARD_ENV_VAR == SCOREBOARD_ENV_VAR

    def test_the_script_runs_the_suite_it_scores(self, scoreboard: ModuleType) -> None:
        """The scored path is this suite, not whatever pytest defaults to."""
        assert scoreboard.SUITE_PATH == "tests/integration/conformance/auditor"
        assert (Path(scoreboard.REPO_ROOT) / scoreboard.SUITE_PATH).is_dir()
