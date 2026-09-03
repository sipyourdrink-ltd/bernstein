"""CLI tests for ``bernstein context segment-prompt`` (#3455 step 1).

A pure, offline debug utility: reads up to four block files, digests them
into named segments plus one ordered segment-list digest, and prints them.
It reads and writes no run state -- anchoring segments in a real run is
later scope for issue #3455.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.context_cmd import context_group

_EMPTY_SHA256 = "sha256:" + hashlib.sha256(b"").hexdigest()


def test_context_segment_prompt_cli_prints_segment_digests(tmp_path: Path) -> None:
    """The subcommand prints each of the four named segments and the list digest."""
    role_file = tmp_path / "role.txt"
    role_file.write_text("You are a backend engineer.", encoding="utf-8")
    task_file = tmp_path / "task.txt"
    task_file.write_text("Fix the widget.", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        context_group,
        ["segment-prompt", "--role-file", str(role_file), "--task-file", str(task_file), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    names = [seg["name"] for seg in payload["segments"]]
    assert names == ["role", "task", "mailbox", "resume"]

    role_digest = next(seg["digest"] for seg in payload["segments"] if seg["name"] == "role")
    assert role_digest == "sha256:" + hashlib.sha256(b"You are a backend engineer.").hexdigest()

    mailbox_digest = next(seg["digest"] for seg in payload["segments"] if seg["name"] == "mailbox")
    assert mailbox_digest == _EMPTY_SHA256

    assert "segments_digest" in payload


def test_context_segment_prompt_cli_writes_nothing_to_disk(tmp_path: Path) -> None:
    """The command touches only the block files it reads -- no .sdd writes."""
    runner = CliRunner()
    result = runner.invoke(context_group, ["segment-prompt", "--json"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".sdd").exists()
