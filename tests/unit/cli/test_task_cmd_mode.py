"""Tests for the ``--mode`` passthrough on task creation (issue #2243).

``bernstein compose --mode <style>`` stamps ``metadata['mode']`` on the
task payload; the spawner's response-style resolver treats that key as the
top-priority resolution input. Both the style vocabulary
(``verbose``/``balanced``/``terse``) and the mode-profile vocabulary
(``fast``/``smart``/``deep``) are accepted.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.task_cmd import add_task


def _payload_from_dry_run(output: str) -> dict:
    """Extract the JSON payload printed by ``--dry-run``."""
    start = output.index("{")
    end = output.rindex("}") + 1
    return json.loads(output[start:end])


class TestAddTaskModeOption:
    @pytest.mark.parametrize("mode", ["verbose", "balanced", "terse", "fast", "smart", "deep"])
    def test_mode_lands_in_metadata(self, mode: str) -> None:
        runner = CliRunner()
        result = runner.invoke(add_task, ["My task", "--mode", mode, "--dry-run"])
        assert result.exit_code == 0, result.output
        payload = _payload_from_dry_run(result.output)
        assert payload["metadata"]["mode"] == mode

    def test_no_mode_leaves_payload_unchanged(self) -> None:
        runner = CliRunner()
        result = runner.invoke(add_task, ["My task", "--dry-run"])
        assert result.exit_code == 0, result.output
        payload = _payload_from_dry_run(result.output)
        assert "metadata" not in payload or "mode" not in payload.get("metadata", {})

    def test_unknown_mode_rejected_before_submission(self) -> None:
        runner = CliRunner()
        result = runner.invoke(add_task, ["My task", "--mode", "shouty", "--dry-run"])
        assert result.exit_code != 0
