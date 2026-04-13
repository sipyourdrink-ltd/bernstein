"""Tests for inline-goal single-agent suggestion UX."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.bootstrap import bootstrap_from_goal


def test_bootstrap_from_goal_prints_single_agent_suggestion(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    def _stop_after_preflight(cli: str, port: int) -> None:
        raise RuntimeError("stop-after-suggestion")

    with patch("bernstein.core.orchestration.bootstrap.preflight_checks", side_effect=_stop_after_preflight):
        with pytest.raises(RuntimeError, match="stop-after-suggestion"):
            bootstrap_from_goal(goal="fix typo in README", workdir=tmp_path)

    captured = capsys.readouterr()
    assert "simple enough for a single-agent session" in captured.out
