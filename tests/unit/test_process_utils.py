"""Tests for process inspection helpers used by shutdown paths."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from bernstein.core.process_utils import is_process_alive, process_cwd


def test_is_process_alive_treats_zombie_as_dead() -> None:
    with (
        patch("bernstein.core.orchestration.process_utils._platform_process_alive", return_value=True),
        patch(
            "bernstein.core.process_utils.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["ps"],
                returncode=0,
                stdout="Z\n",
                stderr="",
            ),
        ),
    ):
        assert is_process_alive(1234) is False


def test_process_cwd_parses_lsof_output() -> None:
    with patch(
        "bernstein.core.process_utils.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["lsof"],
            returncode=0,
            stdout="p1234\nn/Users/sasha/IdeaProjects/personal_projects/bernstein\n",
            stderr="",
        ),
    ):
        cwd = process_cwd(1234)

    assert cwd == Path("/Users/sasha/IdeaProjects/personal_projects/bernstein")
