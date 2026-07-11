"""Tests for ``bernstein`` advanced-command dark paths.

Covers the operator-facing utility commands in ``advanced_cmd`` that are not
exercised elsewhere:

  * ``install-hooks`` - git-hook install, idempotency, --force, no-repo error
  * ``plugins``       - empty + populated + corrupt-meta listing
  * ``github setup`` / ``github test-webhook`` - static guidance output
  * ``ideate``        - server-unreachable exit, --as-json, panel render
  * ``quarantine list`` / ``quarantine clear`` - server-backed flows + confirm
  * ``recap``         - server-unreachable exit, --as-json, table render
  * ``trace`` group   - missing dir, no-trace, legacy alias, reindex, verify

Server-backed commands patch ``server_get`` / ``server_post`` in the command
module's namespace; filesystem commands run inside an isolated cwd.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import (
    github_group,
    ideate,
    install_hooks,
    plugins_cmd,
    quarantine_group,
    recap,
    replay_cmd,
    retro,
    trace_cmd,
)

_SERVER_GET = "bernstein.cli.commands.advanced_cmd.server_get"
_SERVER_POST = "bernstein.cli.commands.advanced_cmd.server_post"


# ---------------------------------------------------------------------------
# install-hooks
# ---------------------------------------------------------------------------


def test_install_hooks_not_a_git_repo_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(install_hooks, [])
    assert result.exit_code == 1, result.output
    assert "Not a git repository" in result.output


def test_install_hooks_writes_executable_hooks() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".git/hooks").mkdir(parents=True)
        result = runner.invoke(install_hooks, [])
        assert result.exit_code == 0, result.output
        assert "Installed" in result.output
        pre_commit = Path(".git/hooks/pre-commit")
        pre_push = Path(".git/hooks/pre-push")
        assert pre_commit.exists()
        assert pre_push.exists()
        # The pre-commit hook actually runs the lint + test gate.
        assert "ruff check" in pre_commit.read_text()
        # Executable bit is set (owner-exec at minimum).
        assert pre_commit.stat().st_mode & 0o100


def test_install_hooks_idempotent_without_force() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".git/hooks").mkdir(parents=True)
        first = runner.invoke(install_hooks, [])
        assert first.exit_code == 0, first.output
        second = runner.invoke(install_hooks, [])
        assert second.exit_code == 0, second.output
        # Second run refuses to overwrite without --force.
        assert "Hook exists" in second.output


def test_install_hooks_force_overwrites() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".git/hooks").mkdir(parents=True)
        runner.invoke(install_hooks, [])
        # Mutate the existing hook so we can prove --force rewrites it.
        Path(".git/hooks/pre-commit").write_text("# stale\n")
        result = runner.invoke(install_hooks, ["--force"])
        assert result.exit_code == 0, result.output
        assert "Installed" in result.output
        assert "ruff check" in Path(".git/hooks/pre-commit").read_text()


# ---------------------------------------------------------------------------
# plugins
# ---------------------------------------------------------------------------


def test_plugins_no_directory() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(plugins_cmd, [])
    assert result.exit_code == 0, result.output
    assert "No plugins directory found" in result.output


def test_plugins_lists_installed_with_metadata() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        pd = Path(".bernstein/plugins/myplugin")
        pd.mkdir(parents=True)
        (pd / "meta.json").write_text(json.dumps({"version": "1.2.3", "type": "agent"}))
        result = runner.invoke(plugins_cmd, [])
    assert result.exit_code == 0, result.output
    assert "myplugin" in result.output
    assert "1.2.3" in result.output


def test_plugins_corrupt_meta_still_listed() -> None:
    """A plugin with unparseable meta.json is still listed (with '?' fields)."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        pd = Path(".bernstein/plugins/broken")
        pd.mkdir(parents=True)
        (pd / "meta.json").write_text("not valid json{")
        result = runner.invoke(plugins_cmd, [])
    assert result.exit_code == 0, result.output
    assert "broken" in result.output


# ---------------------------------------------------------------------------
# github group
# ---------------------------------------------------------------------------


def test_github_setup_prints_env_guidance() -> None:
    runner = CliRunner()
    result = runner.invoke(github_group, ["setup"])
    assert result.exit_code == 0, result.output
    assert "GITHUB_TOKEN" in result.output
    assert "GITHUB_REPO" in result.output


def test_github_test_webhook_reports_configured() -> None:
    runner = CliRunner()
    result = runner.invoke(github_group, ["test-webhook"])
    assert result.exit_code == 0, result.output
    assert "Webhook configured" in result.output


# ---------------------------------------------------------------------------
# ideate
# ---------------------------------------------------------------------------


def test_ideate_server_unreachable_exits_one() -> None:
    runner = CliRunner()
    with patch(_SERVER_GET, return_value=None):
        result = runner.invoke(ideate, [])
    assert result.exit_code == 1, result.output


def test_ideate_as_json_emits_ideas() -> None:
    ideas = {"ideas": [{"title": "Speed up", "description": "cache it"}, {"title": "B", "description": "b"}]}
    runner = CliRunner()
    with patch(_SERVER_GET, return_value=ideas):
        result = runner.invoke(ideate, ["--count", "1", "--as-json"])
    assert result.exit_code == 0, result.output
    assert "Speed up" in result.output


def test_ideate_panel_render_respects_count() -> None:
    ideas = {"ideas": [{"title": f"Idea{i}", "description": "d"} for i in range(5)]}
    runner = CliRunner()
    with patch(_SERVER_GET, return_value=ideas):
        result = runner.invoke(ideate, ["--count", "2"])
    assert result.exit_code == 0, result.output
    assert "Idea 1" in result.output
    assert "Idea 2" in result.output


# ---------------------------------------------------------------------------
# quarantine
# ---------------------------------------------------------------------------


def test_quarantine_list_server_unreachable_exits_one() -> None:
    runner = CliRunner()
    with patch(_SERVER_GET, return_value=None):
        result = runner.invoke(quarantine_group, ["list"])
    assert result.exit_code == 1, result.output


def test_quarantine_list_empty() -> None:
    runner = CliRunner()
    with patch(_SERVER_GET, return_value={"tasks": []}):
        result = runner.invoke(quarantine_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "No quarantined tasks" in result.output


def test_quarantine_list_renders_rows() -> None:
    data = {"tasks": [{"id": "t1", "title": "Failed task", "reason": "timeout"}]}
    runner = CliRunner()
    with patch(_SERVER_GET, return_value=data):
        result = runner.invoke(quarantine_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "Failed task" in result.output
    assert "timeout" in result.output


def test_quarantine_clear_with_confirm_flag() -> None:
    runner = CliRunner()
    with patch(_SERVER_POST, return_value={"cleared": 3}):
        result = runner.invoke(quarantine_group, ["clear", "--confirm"])
    assert result.exit_code == 0, result.output
    assert "Cleared 3 task" in result.output


def test_quarantine_clear_declined_at_prompt() -> None:
    runner = CliRunner()
    # Answer "n" to the confirmation prompt -> no server call, cancelled.
    with patch(_SERVER_POST) as mock_post:
        result = runner.invoke(quarantine_group, ["clear"], input="n\n")
    # Declining must short-circuit before any server call is made.
    mock_post.assert_not_called()
    assert result.exit_code == 0, result.output
    assert "Cancelled" in result.output


def test_quarantine_clear_server_unreachable_exits_one() -> None:
    runner = CliRunner()
    with patch(_SERVER_POST, return_value=None):
        result = runner.invoke(quarantine_group, ["clear", "--confirm"])
    assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# recap
# ---------------------------------------------------------------------------


def test_recap_server_unreachable_exits_one() -> None:
    runner = CliRunner()
    with patch(_SERVER_GET, return_value=None):
        result = runner.invoke(recap, [])
    assert result.exit_code == 1, result.output


def test_recap_as_json() -> None:
    data = {"summary": {"total": 5, "completed": 4, "failed": 1, "success_rate": 80.0}}
    runner = CliRunner()
    with patch(_SERVER_GET, return_value=data):
        result = runner.invoke(recap, ["--as-json"])
    assert result.exit_code == 0, result.output
    assert "80" in result.output


def test_recap_table_render() -> None:
    data = {"summary": {"total": 5, "completed": 4, "failed": 1, "success_rate": 80.0}}
    runner = CliRunner()
    with patch(_SERVER_GET, return_value=data):
        result = runner.invoke(recap, [])
    assert result.exit_code == 0, result.output
    assert "Success rate" in result.output
    assert "80.0%" in result.output


# ---------------------------------------------------------------------------
# trace group
# ---------------------------------------------------------------------------


def test_trace_no_subcommand_prints_help() -> None:
    runner = CliRunner()
    result = runner.invoke(trace_cmd, [])
    assert result.exit_code == 0, result.output
    # invoke_without_command prints the group help.
    assert "serve" in result.output
    assert "verify" in result.output


def test_trace_show_missing_dir_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(trace_cmd, ["--traces-dir", "nope", "show", "task-123"])
    assert result.exit_code == 1, result.output
    assert "not found" in result.output.lower()


def test_trace_show_no_match_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".sdd/traces").mkdir(parents=True)
        result = runner.invoke(trace_cmd, ["show", "task-zzz"])
    assert result.exit_code == 1, result.output
    assert "No trace found" in result.output


def test_trace_legacy_alias_resolves_to_show() -> None:
    """``bernstein trace <task-id>`` (no 'show') still prints the trace."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        td = Path(".sdd/traces")
        td.mkdir(parents=True)
        (td / "trace-task-abc.json").write_text(json.dumps({"task": "abc", "events": [1, 2]}))
        result = runner.invoke(trace_cmd, ["task-abc", "--as-json"])
    assert result.exit_code == 0, result.output
    assert "abc" in result.output


def test_trace_reindex_reports_count() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".sdd/traces").mkdir(parents=True)
        result = runner.invoke(trace_cmd, ["reindex"])
    assert result.exit_code == 0, result.output
    assert "Reindex complete" in result.output


def test_trace_verify_missing_trace_fails() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".sdd/traces").mkdir(parents=True)
        result = runner.invoke(trace_cmd, ["verify", "deadbeefcafe"])
    assert result.exit_code == 1, result.output
    assert "FAIL" in result.output


# ---------------------------------------------------------------------------
# replay (pseudo-subcommand dispatcher)
# ---------------------------------------------------------------------------


def test_replay_requires_an_argument() -> None:
    runner = CliRunner()
    result = runner.invoke(replay_cmd, [])
    # nargs=-1 + required=True -> click rejects the empty invocation.
    assert result.exit_code == 2, result.output


def test_replay_too_many_positionals_is_usage_error() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(replay_cmd, ["a", "b", "c"])
    assert result.exit_code == 2, result.output
    assert "Usage" in result.output


def test_replay_list_empty() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(replay_cmd, ["list"])
    assert result.exit_code == 0, result.output
    assert "No runs recorded yet" in result.output


def test_replay_list_shows_recorded_run() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        rd = Path(".sdd/runs/20240315-143022")
        rd.mkdir(parents=True)
        (rd / "journal.jsonl").write_text(json.dumps({"ts": 1.0, "event": "start"}) + "\n")
        result = runner.invoke(replay_cmd, ["list"])
    assert result.exit_code == 0, result.output
    assert "20240315-143022" in result.output


def test_replay_latest_with_no_runs_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(replay_cmd, ["latest"])
    assert result.exit_code == 1, result.output
    assert "No replay logs found" in result.output


def test_replay_diff_requires_two_runs() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(replay_cmd, ["diff", "only-one"])
    assert result.exit_code == 2, result.output
    assert "RUN_A RUN_B" in result.output


def test_replay_diff_missing_runs_exits_two() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(replay_cmd, ["diff", "runA", "runB"])
    assert result.exit_code == 2, result.output
    assert "not found" in result.output.lower()


def test_replay_export_without_agent_is_usage_error() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(replay_cmd, ["export"])
    assert result.exit_code == 2, result.output
    assert "Usage" in result.output


def test_replay_verify_without_receipt_is_usage_error() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(replay_cmd, ["verify"])
    assert result.exit_code == 2, result.output
    assert "RECEIPT" in result.output


# ---------------------------------------------------------------------------
# retro
# ---------------------------------------------------------------------------


def test_retro_no_archive_reports_no_tasks() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(retro, [])
    assert result.exit_code == 0, result.output
    assert "No tasks found" in result.output


def test_retro_writes_default_report() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        ad = Path(".sdd/archive")
        ad.mkdir(parents=True)
        rows = [
            {"id": "t1", "title": "Build", "status": "done", "role": "backend"},
            {"id": "t2", "title": "Test", "status": "failed", "role": "qa"},
        ]
        (ad / "tasks.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        result = runner.invoke(retro, [])
        assert result.exit_code == 0, result.output
        assert "Retrospective saved" in result.output
        # The default report lands under .sdd/runtime/.
        assert Path(".sdd/runtime/retrospective.md").exists()


def test_retro_custom_output_and_print() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        ad = Path(".sdd/archive")
        ad.mkdir(parents=True)
        (ad / "tasks.jsonl").write_text(
            json.dumps({"id": "t1", "title": "Build", "status": "done", "role": "backend"}) + "\n"
        )
        result = runner.invoke(retro, ["-o", "myreport.md", "--print"])
        assert result.exit_code == 0, result.output
        # --output redirects the report file.
        assert Path("myreport.md").exists()
        # --print echoes the report body to stdout (a markdown heading is present).
        assert "#" in result.output


def test_retro_since_filter_excludes_old_tasks() -> None:
    """``--since`` filters by a recency window; an old-only archive is empty."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        ad = Path(".sdd/archive")
        ad.mkdir(parents=True)
        # A task with a very old completion timestamp (epoch ~ 2001).
        (ad / "tasks.jsonl").write_text(
            json.dumps(
                {
                    "id": "old",
                    "title": "Ancient",
                    "status": "done",
                    "role": "backend",
                    "completed_at": 1_000_000_000.0,
                }
            )
            + "\n"
        )
        result = runner.invoke(retro, ["--since", "1"])
    assert result.exit_code == 0, result.output
    assert "No tasks found" in result.output
