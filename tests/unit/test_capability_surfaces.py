"""Reachability tests for capabilities wired up in issue #2973.

Each test drives the capability through the surface an operator actually
uses - the spawn prompt renderer, a CLI invocation, the TUI log widget, a
task-server route - rather than by importing the implementing module and
calling it directly. Importing the module proves the code runs; these prove
it is *connected*.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from bernstein.core.models import Task
from bernstein.core.spawner import _render_prompt as render_spawn_prompt
from click.testing import CliRunner

from bernstein.cli.commands.agents_cmd import agents_group
from bernstein.cli.commands.diff_cmd import diff_cmd
from bernstein.cli.commands.security_review_cmd import security_review_cmd
from bernstein.cli.main import cli

_SAMPLE_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,6 +1,6 @@
-value = compute(alpha, beta)
+value = compute(alpha, gamma)
 keep_1 = 1
 keep_2 = 2
 keep_3 = 3
 keep_4 = 4
 keep_5 = 5
"""


# ---------------------------------------------------------------------------
# Output style customization -> spawn prompt
# ---------------------------------------------------------------------------


def _write_style(workdir: Path, name: str = "terse") -> None:
    styles = workdir / ".bernstein" / "output-styles"
    styles.mkdir(parents=True, exist_ok=True)
    (styles / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: Answer in one paragraph.\nterse_mode: true\n---\nbody\n",
        encoding="utf-8",
    )


def test_spawn_prompt_carries_the_workspace_output_style(tmp_path: Path) -> None:
    """A style defined in the workspace reaches the prompt an agent is spawned with."""
    _write_style(tmp_path)
    tasks = [Task(id="T1", title="t", description="d", role="backend")]

    prompt = render_spawn_prompt(tasks=tasks, templates_dir=tmp_path, workdir=tmp_path)

    assert "## Output style" in prompt
    assert "Answer in one paragraph." in prompt
    assert "Use terse/concise output format." in prompt


def test_spawn_prompt_honours_the_selected_style(tmp_path: Path) -> None:
    """``output_style:`` in bernstein.yaml selects which style is injected."""
    _write_style(tmp_path, "terse")
    _write_style(tmp_path, "detailed")
    (tmp_path / "bernstein.yaml").write_text("output_style: detailed\n", encoding="utf-8")
    tasks = [Task(id="T1", title="t", description="d", role="backend")]

    prompt = render_spawn_prompt(tasks=tasks, templates_dir=tmp_path, workdir=tmp_path)

    assert "Output style: detailed" in prompt
    assert "Output style: terse" not in prompt


def test_spawn_prompt_has_no_style_section_without_styles(tmp_path: Path) -> None:
    """Workspaces that define no styles get no section at all."""
    tasks = [Task(id="T1", title="t", description="d", role="backend")]

    prompt = render_spawn_prompt(tasks=tasks, templates_dir=tmp_path, workdir=tmp_path)

    assert "## Output style" not in prompt


# ---------------------------------------------------------------------------
# Diff folding + word-level diff -> ``bernstein diff``
# ---------------------------------------------------------------------------


def _run_diff(args: list[str]) -> str:
    resolved = "bernstein.cli.commands.diff_cmd.resolve_diff"
    from bernstein.cli.commands.diff_cmd import ResolvedDiff

    runner = CliRunner()
    with patch(resolved, return_value=ResolvedDiff(diff_text=_SAMPLE_DIFF, source_label="")):
        result = runner.invoke(diff_cmd, ["T1", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_diff_fold_collapses_hunks_through_the_cli() -> None:
    """``bernstein diff --fold`` renders the folded summary, not the raw hunk."""
    output = _run_diff(["--fold", "--fold-lines", "2"])

    assert "app.py" in output
    assert "lines folded" in output or "more lines" in output
    assert "keep_5 = 5" not in output


def test_diff_without_fold_shows_every_line() -> None:
    """The default rendering is unchanged: no folding unless asked for."""
    output = _run_diff([])

    assert "keep_5 = 5" in output


def test_diff_word_diff_highlights_only_changed_tokens() -> None:
    """``bernstein diff --word-diff`` pairs -/+ lines and marks the changed word."""
    output = _run_diff(["--word-diff"])

    assert "alpha" in output
    assert "beta" in output
    assert "gamma" in output
    # Both sides of the replaced line render together, one after the other.
    assert output.index("beta") < output.index("gamma")


# ---------------------------------------------------------------------------
# Diff folding -> TUI agent log
# ---------------------------------------------------------------------------


def test_agent_log_widget_folds_historical_diffs() -> None:
    """The TUI log widget folds a long diff in the historical tail."""
    from bernstein.tui.agent_log import AgentLogWidget

    widget = AgentLogWidget()
    written: list[str] = []
    widget.write = lambda renderable, *a, **k: written.append(str(renderable))  # type: ignore[method-assign]

    long_diff = ["diff --git a/big.py b/big.py", "--- a/big.py", "+++ b/big.py"]
    long_diff += [f"+line {i}" for i in range(60)]
    widget.load_historical_lines(long_diff)

    body = "\n".join(written)
    assert "lines folded" in body
    assert "line 59" not in body


def test_agent_log_widget_can_opt_out_of_folding() -> None:
    """Folding is a default, not a lock-in."""
    from bernstein.tui.agent_log import AgentLogWidget

    widget = AgentLogWidget()
    written: list[str] = []
    widget.write = lambda renderable, *a, **k: written.append(str(renderable))  # type: ignore[method-assign]

    long_diff = ["diff --git a/big.py b/big.py", "--- a/big.py", "+++ b/big.py"]
    long_diff += [f"+line {i}" for i in range(60)]
    widget.load_historical_lines(long_diff, fold_diffs=False)

    assert "line 59" in "\n".join(written)


# ---------------------------------------------------------------------------
# Security review -> ``bernstein security-review``
# ---------------------------------------------------------------------------


def test_security_review_command_flags_a_hardcoded_key() -> None:
    """The command scans a piped diff and exits non-zero on a critical finding."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("bad.diff").write_text(
            "diff --git a/s.py b/s.py\n--- a/s.py\n+++ b/s.py\n@@ -0,0 +1 @@\n+KEY = 'AKIAIOSFODNN7EXAMPLE'\n",
            encoding="utf-8",
        )
        result = runner.invoke(security_review_cmd, ["--diff-file", "bad.diff", "--as-json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["blocked"] is True
    assert any(f["severity"] == "critical" for f in payload["findings"])


def test_security_review_command_passes_a_clean_diff() -> None:
    """A clean diff exits zero and reports no findings."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("ok.diff").write_text(
            "diff --git a/s.py b/s.py\n--- a/s.py\n+++ b/s.py\n@@ -0,0 +1 @@\n+total = 1 + 2\n",
            encoding="utf-8",
        )
        result = runner.invoke(security_review_cmd, ["--diff-file", "ok.diff", "--as-json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["total_findings"] == 0


def test_security_review_command_reports_no_diff_distinctly() -> None:
    """Nothing to scan exits 2, so a gate can tell it apart from 'clean'."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("empty.diff").write_text("", encoding="utf-8")
        result = runner.invoke(security_review_cmd, ["--diff-file", "empty.diff"])

    assert result.exit_code == 2, result.output


def test_security_review_is_registered_on_the_root_cli() -> None:
    """The command is reachable as ``bernstein security-review``."""
    assert "security-review" in cli.commands


# ---------------------------------------------------------------------------
# Plugin trust -> ``bernstein plugins``
# ---------------------------------------------------------------------------


def test_plugins_listing_reports_trust_and_warns_on_unknown() -> None:
    """An unsigned, undocumented plugin is listed as low trust with a warning."""
    from bernstein.cli.commands.advanced_cmd import plugins_cmd

    runner = CliRunner()
    with runner.isolated_filesystem():
        plugin = Path(".bernstein/plugins/sketchy")
        plugin.mkdir(parents=True)
        (plugin / "meta.json").write_text(json.dumps({"version": "0.1.0", "type": "agent"}), encoding="utf-8")
        result = runner.invoke(plugins_cmd, [])

    assert result.exit_code == 0, result.output
    assert "sketchy" in result.output
    assert "unknown" in result.output
    assert "WARNING" in result.output


def test_plugins_listing_does_not_warn_on_a_well_signalled_plugin() -> None:
    """A plugin with a signature, packaging metadata, and tests scores higher."""
    from bernstein.cli.commands.advanced_cmd import plugins_cmd

    runner = CliRunner()
    with runner.isolated_filesystem():
        plugin = Path(".bernstein/plugins/solid")
        (plugin / "tests").mkdir(parents=True)
        (plugin / "meta.json").write_text(json.dumps({"version": "1.0.0", "type": "agent"}), encoding="utf-8")
        (plugin / ".signature").write_text("sig", encoding="utf-8")
        (plugin / "README.md").write_text("docs", encoding="utf-8")
        (plugin / "pyproject.toml").write_text(
            '[project]\nname = "solid"\nversion = "1.0.0"\nauthor = "x"\n', encoding="utf-8"
        )
        result = runner.invoke(plugins_cmd, [])

    assert result.exit_code == 0, result.output
    assert "trusted" in result.output
    assert "WARNING" not in result.output


# ---------------------------------------------------------------------------
# Away summary -> ``bernstein recap --since``
# ---------------------------------------------------------------------------


def test_recap_since_reports_workspace_events_without_a_server() -> None:
    """``recap --since`` reads the workspace journal and needs no task server."""
    from bernstein.cli.commands.advanced_cmd import recap

    runner = CliRunner()
    with runner.isolated_filesystem():
        journal = Path(".sdd/runtime/tasks.jsonl")
        journal.parent.mkdir(parents=True)
        import time as _time

        now = _time.time()
        journal.write_text(
            "\n".join(
                [
                    json.dumps({"id": "T1", "title": "ship it", "status": "done", "timestamp": now}),
                    json.dumps({"id": "T2", "title": "broke it", "status": "failed", "timestamp": now}),
                ]
            ),
            encoding="utf-8",
        )
        result = runner.invoke(recap, ["--since", "2h", "--as-json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["completed_tasks"] == 1
    assert payload["failed_tasks"] == 1


def test_recap_since_rejects_a_malformed_duration() -> None:
    """A typo in --since fails fast instead of silently reporting nothing."""
    from bernstein.cli.commands.advanced_cmd import recap

    runner = CliRunner()
    result = runner.invoke(recap, ["--since", "soon"])

    assert result.exit_code != 0
    assert "duration" in result.output.lower()


# ---------------------------------------------------------------------------
# Agent trust tiers -> task route + ``bernstein agents trust``
# ---------------------------------------------------------------------------


def test_agents_trust_reports_nothing_before_any_task_runs() -> None:
    """An untouched workspace says so rather than inventing rows."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(agents_group, ["trust"])

    assert result.exit_code == 0, result.output
    assert "No agent trust records" in result.output


def test_agents_trust_surfaces_outcomes_recorded_by_the_task_route(tmp_path: Path) -> None:
    """A completed task on the server shows up in ``bernstein agents trust``."""
    from bernstein.core.agents.agent_trust import AgentTrustStore

    sdd_dir = tmp_path / ".sdd"
    # The route helper is the only writer; drive it the way the route does.
    AgentTrustStore(sdd_dir).record_task_outcome("backend", success=True)

    runner = CliRunner()
    result = runner.invoke(agents_group, ["trust", "--workdir", str(tmp_path), "--as-json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["agent_id"] for row in payload] == ["backend"]
    assert payload[0]["tasks_completed"] == 1


# ---------------------------------------------------------------------------
# Contextual tips -> CLI group result callback
# ---------------------------------------------------------------------------


def test_tips_are_suppressed_outside_a_workspace() -> None:
    """No ``.sdd/`` means no cooldown marker to write, so no tip."""
    from bernstein.cli.main import tips_enabled

    runner = CliRunner()
    with runner.isolated_filesystem(), patch("sys.stdout.isatty", return_value=True):
        import click

        ctx = click.Context(cli)
        ctx.obj = {}
        assert tips_enabled(ctx) is False


def test_tips_are_suppressed_for_json_output_and_opt_out(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Machine-readable output and the opt-out env var both silence tips."""
    import click

    from bernstein.cli.main import TIPS_OPT_OUT_ENV_VAR, tips_enabled

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".sdd").mkdir()

    with patch("sys.stdout.isatty", return_value=True):
        ctx = click.Context(cli)
        ctx.obj = {"JSON": True}
        assert tips_enabled(ctx) is False

        ctx.obj = {}
        assert tips_enabled(ctx) is True

        monkeypatch.setenv(TIPS_OPT_OUT_ENV_VAR, "1")
        assert tips_enabled(ctx) is False


def test_tip_is_printed_after_a_real_subcommand_invocation(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Running a subcommand through the root group emits one tip afterwards."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".sdd").mkdir()
    monkeypatch.delenv("BERNSTEIN_NO_TIPS", raising=False)

    runner = CliRunner()
    with patch("bernstein.cli.main.tips_enabled", return_value=True):
        first = runner.invoke(cli, ["agents", "trust"])
        second = runner.invoke(cli, ["agents", "trust"])

    assert first.exit_code == 0, first.output
    assert "bernstein" in first.output
    assert (tmp_path / ".sdd" / "tips" / "last_shown").exists()
    # The cooldown marker suppresses the very next invocation.
    assert len(second.output) < len(first.output)


def test_no_tip_when_tips_are_disabled(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """With tips disabled the command output is untouched and no marker is written."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".sdd").mkdir()

    runner = CliRunner()
    with patch("bernstein.cli.main.tips_enabled", return_value=False):
        result = runner.invoke(cli, ["agents", "trust"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".sdd" / "tips" / "last_shown").exists()
