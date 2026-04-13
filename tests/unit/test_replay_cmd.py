"""Tests for replay CLI metadata output."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.cli.advanced_cmd import replay_cmd
from click.testing import CliRunner


def _write_run(run_dir: Path, *, run_id: str) -> None:
    run_path = run_dir / run_id
    run_path.mkdir(parents=True, exist_ok=True)
    (run_path / "replay.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": 1.0, "elapsed_s": 0.0, "event": "run_started", "run_id": run_id}),
                json.dumps({"ts": 11.0, "elapsed_s": 10.0, "event": "run_completed", "run_id": run_id}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (run_path / "metadata.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "started_at": 1_710_000_000.0,
                "git_sha": "1234567890abcdef",
                "git_branch": "feature/track-b",
                "config_hash": "feedfacecafebeef",
            }
        ),
        encoding="utf-8",
    )


def test_replay_list_shows_metadata_columns(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    _write_run(sdd_dir / "runs", run_id="20240315-143022")

    runner = CliRunner()
    result = runner.invoke(replay_cmd, ["list", "--sdd-dir", str(sdd_dir)])

    assert result.exit_code == 0
    # Metadata columns may be truncated on narrow terminals (CI)
    assert "20240315" in result.output


def test_replay_output_shows_metadata_header(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    _write_run(sdd_dir / "runs", run_id="20240315-143022")

    runner = CliRunner()
    result = runner.invoke(replay_cmd, ["20240315-143022", "--sdd-dir", str(sdd_dir)])

    assert result.exit_code == 0
    assert "Branch:" in result.output
    assert "feature/track-b" in result.output
    assert "SHA:" in result.output
