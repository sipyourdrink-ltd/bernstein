"""Tests for replay CLI metadata output."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.cli.advanced_cmd import replay_cmd
from click.testing import CliRunner

from bernstein.core.persistence.journal import Journal


def _populate_agent_journal(sdd_dir: Path, agent_id: str, n: int = 2) -> str:
    """Seed a per-step journal for *agent_id* under *sdd_dir*; return its head hash."""
    journal_dir = sdd_dir / "runtime" / "journal" / agent_id
    journal = Journal.open(journal_dir)
    head = ""
    for i in range(n):
        entry = journal.append(input_hash=f"a{i}", model="m1", prompt=f"p{i}")
        head = entry.step_hash
    journal.close()
    return head


def _write_run(run_dir: Path, *, run_id: str) -> None:
    run_path = run_dir / run_id
    run_path.mkdir(parents=True, exist_ok=True)
    (run_path / "journal.jsonl").write_text(
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


# ---------------------------------------------------------------------------
# issue #3976: export/publish/verify flag defects
# ---------------------------------------------------------------------------


def test_export_usage_string_matches_the_positional_invocation(tmp_path: Path) -> None:
    """``export`` with no AGENT_ID must show the real (positional) shape.

    The dispatcher has never accepted ``-o``; the destination is the third
    positional. The old usage string advertised a flag that does not exist.
    """
    sdd_dir = tmp_path / ".sdd"

    result = CliRunner().invoke(replay_cmd, ["export", "--sdd-dir", str(sdd_dir)])

    assert result.exit_code == 2
    assert "-o" not in result.output
    assert "bernstein replay export <AGENT_ID> [OUT]" in result.output


def test_publish_confirmation_flag_actually_completes_a_publish(tmp_path: Path) -> None:
    """The only spelling the refusal message names must actually work.

    Before the fix, click declared neither ``--yes-i-want-to-publish`` (named
    by the refusal message) nor ``--opt-in`` (checked by the dispatcher), so
    ``publish`` could never exit 0.
    """
    sdd_dir = tmp_path / ".sdd"
    _populate_agent_journal(sdd_dir, "agent-1", 1)
    out = tmp_path / "redacted.tar"

    result = CliRunner().invoke(
        replay_cmd,
        ["publish", "agent-1", str(out), "--yes-i-want-to-publish", "--sdd-dir", str(sdd_dir)],
    )

    assert result.exit_code == 0, result.output
    assert out.exists()


def test_publish_without_confirmation_still_refuses(tmp_path: Path) -> None:
    """No flag at all keeps refusing, and names the real flag in the message."""
    sdd_dir = tmp_path / ".sdd"
    _populate_agent_journal(sdd_dir, "agent-1", 1)
    out = tmp_path / "redacted.tar"

    result = CliRunner().invoke(
        replay_cmd,
        ["publish", "agent-1", str(out), "--sdd-dir", str(sdd_dir)],
    )

    assert result.exit_code == 2
    assert "--yes-i-want-to-publish" in result.output
    assert not out.exists()


def test_dead_opt_in_flag_is_no_longer_accepted_anywhere(tmp_path: Path) -> None:
    """``--opt-in`` named nothing reachable even before the fix; it is now gone."""
    sdd_dir = tmp_path / ".sdd"
    _populate_agent_journal(sdd_dir, "agent-1", 1)
    out = tmp_path / "redacted.tar"

    result = CliRunner().invoke(
        replay_cmd,
        ["publish", "agent-1", str(out), "--opt-in", "--sdd-dir", str(sdd_dir)],
    )

    assert result.exit_code != 0
    assert not out.exists()


def test_the_confirmation_flag_on_any_other_verb_is_a_named_error(tmp_path: Path) -> None:
    """Declaring ``--yes-i-want-to-publish`` command-wide must not let other
    verbs silently accept it -- that would recreate the accepted-but-ignored
    ``--as-json`` defect this same issue is about."""
    sdd_dir = tmp_path / ".sdd"
    _populate_agent_journal(sdd_dir, "agent-1", 1)
    out = tmp_path / "out.tar"

    result = CliRunner().invoke(
        replay_cmd,
        ["export", "agent-1", str(out), "--yes-i-want-to-publish", "--sdd-dir", str(sdd_dir)],
    )

    assert result.exit_code == 2
    assert "--yes-i-want-to-publish" in result.output
    assert not out.exists()


def test_the_confirmation_flag_on_plain_run_replay_is_also_a_named_error(tmp_path: Path) -> None:
    """The guard is verb-agnostic: it fires even off the legacy run-id path."""
    sdd_dir = tmp_path / ".sdd"

    result = CliRunner().invoke(
        replay_cmd,
        ["some-run-id", "--yes-i-want-to-publish", "--sdd-dir", str(sdd_dir)],
    )

    assert result.exit_code == 2
    assert "--yes-i-want-to-publish" in result.output


def test_as_json_on_export_emits_json_not_silence(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    _populate_agent_journal(sdd_dir, "agent-1", 2)
    out = tmp_path / "out.tar"

    result = CliRunner().invoke(
        replay_cmd,
        ["export", "agent-1", str(out), "--as-json", "--sdd-dir", str(sdd_dir)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["agent_id"] == "agent-1"
    assert payload["output"] == str(out)
    assert payload["head_hash"]
    assert payload["steps"] == 2


def test_as_json_on_publish_emits_json_not_silence(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    _populate_agent_journal(sdd_dir, "agent-1", 1)
    out = tmp_path / "redacted.tar"

    result = CliRunner().invoke(
        replay_cmd,
        [
            "publish",
            "agent-1",
            str(out),
            "--yes-i-want-to-publish",
            "--as-json",
            "--sdd-dir",
            str(sdd_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["agent_id"] == "agent-1"
    assert payload["output"] == str(out)
    assert payload["head_hash"]
    assert payload["original_head_hash"]


def test_as_json_on_receipt_verify_emits_the_verification_result(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    head = _populate_agent_journal(sdd_dir, "agent-1", 2)
    out = tmp_path / "out.tar"
    runner = CliRunner()
    export = runner.invoke(replay_cmd, ["export", "agent-1", str(out), "--sdd-dir", str(sdd_dir)])
    assert export.exit_code == 0, export.output

    result = runner.invoke(replay_cmd, ["verify", str(out), "--as-json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["head_hash"] == head
