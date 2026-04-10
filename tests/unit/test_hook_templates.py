"""Tests for bundled command-hook templates."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.templates_cmd import templates_group
from bernstein.core.hook_templates import get_hook_template, list_hook_templates, scaffold_hook_template


def test_list_hook_templates_contains_expected_templates() -> None:
    names = [template.name for template in list_hook_templates()]
    assert names == ["slack-notify", "pagerduty-alert", "jira-update"]


def test_scaffold_hook_template_creates_files(tmp_path: Path) -> None:
    created = scaffold_hook_template("slack-notify", tmp_path)

    rel_paths = sorted(path.relative_to(tmp_path).as_posix() for path in created)
    assert rel_paths == [
        ".bernstein/hooks/README.slack-notify.md",
        ".bernstein/hooks/on_task_completed/slack_notify.py",
        ".bernstein/hooks/on_task_failed/slack_notify.py",
    ]
    assert (tmp_path / rel_paths[1]).stat().st_mode & 0o111


def test_scaffold_hook_template_rejects_overwrite_without_force(tmp_path: Path) -> None:
    scaffold_hook_template("jira-update", tmp_path)

    try:
        scaffold_hook_template("jira-update", tmp_path)
    except FileExistsError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("Expected scaffold_hook_template() to reject overwrite")


def test_templates_hooks_use_cli_installs_template(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(templates_group, ["hooks", "use", "pagerduty-alert", "--workdir", str(tmp_path)])

    assert result.exit_code == 0
    assert "Installed hook template" in result.output
    assert (tmp_path / ".bernstein" / "hooks" / "on_task_failed" / "pagerduty_alert.py").exists()


def test_templates_hooks_list_cli_outputs_descriptions() -> None:
    runner = CliRunner()

    result = runner.invoke(templates_group, ["hooks", "list"])

    assert result.exit_code == 0
    assert "slack-notify" in result.output
    assert "PagerDuty" in result.output


def test_get_hook_template_unknown_returns_none() -> None:
    assert get_hook_template("does-not-exist") is None
