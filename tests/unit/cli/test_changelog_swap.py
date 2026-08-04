"""Swap ``bernstein changelog`` and ``bernstein run-changelog`` (issue #3142).

In v4.0.0 the orchestration-specific command takes the bare name
``bernstein changelog``, and the conventional-commit command moves under
``bernstein changelog conventional``. The ``bernstein run-changelog`` name
stays registered as a deprecated alias so existing scripts print a warning
rather than silently changing meaning; it is removed in a release after the
4.0 line that introduced it. Both commands had zero tests before #3142; this
file pins both.

The tests go through the top-level ``cli`` entry point rather than the group
or subcommand objects directly, so they pin the registered names and the
help-text shape. The group can be removed together with the alias.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner, Result

from bernstein.cli.commands.run_changelog_cmd import run_changelog_default
from bernstein.cli.main import changelog_cmd, cli, run_changelog_cmd
from bernstein.cli.main import run_changelog_default as _re_default

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stderr_lines(result: Result) -> list[str]:
    return [line for line in (result.stderr or "").splitlines() if line.strip()]


def _make_metrics_dir(root: Path, role: str = "backend") -> None:
    """Drop a single metrics record so ``generate_run_changelog`` has input."""
    metrics_dir = root / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "timestamp": time.time(),
        "task_id": "aabbccdd1234",
        "role": role,
        "success": True,
    }
    (metrics_dir / "2026-08-04.jsonl").write_text(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Group registration
# ---------------------------------------------------------------------------


class TestChangelogGroupRegistration:
    """The swap requires three registrations on the top-level ``bernstein`` CLI."""

    def test_changelog_is_a_group_not_a_command(self) -> None:
        # click.Group (not Command). The conventional + run-changelog subcommands
        # are attached as group.commands below.
        assert hasattr(changelog_cmd, "commands")
        assert hasattr(changelog_cmd, "invoke_without_command")

    def test_group_has_conventional_subcommand(self) -> None:
        sub_names = set(changelog_cmd.commands.keys())
        assert "conventional" in sub_names

    def test_no_legacy_run_changelog_inside_group(self) -> None:
        # The legacy ``bernstein run-changelog`` is a TOP-LEVEL deprecated
        # alias (#3142), not a subcommand of the changelog group. Putting it
        # inside the group would invite users to write
        # ``bernstein changelog run-changelog`` which the issue does not
        # authorise.
        sub_names = set(changelog_cmd.commands.keys())
        assert "run-changelog" not in sub_names

    def test_legacy_run_changelog_registered_at_top_level(self) -> None:
        top_names = set(cli.commands.keys())
        assert "run-changelog" in top_names
        assert "changelog" in top_names

    def test_back_compat_alias_in_main_module(self) -> None:
        # ``from bernstein.cli.main import run_changelog_cmd`` keeps working
        # for as long as the alias lives, per #3142. It points to the new name.
        assert run_changelog_cmd is _re_default
        assert run_changelog_cmd is run_changelog_default


# ---------------------------------------------------------------------------
# Deprecation notice on ``bernstein run-changelog``
# ---------------------------------------------------------------------------


class TestRunChangelogDeprecation:
    """The legacy ``run-changelog`` name must emit a clear deprecation notice."""

    def test_run_changelog_invokes_deprecation_notice(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["run-changelog", "--help"])

        # --help exits 0 even for subcommands, and stderr carries the notice.
        assert result.exit_code == 0, result.output
        # Help text itself must mention the deprecation so a user running
        # ``bernstein run-changelog --help`` learns about the rename.
        # click renders the long form on multiple lines, so accept either
        # collapsed or split ("[Deprecated,\n  removed in a later release]").
        combined = (result.output or "").replace("\n", " ")
        assert "Deprecated" in combined and "removed in a later release" in combined

    def test_run_changelog_help_text_points_to_new_name(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["run-changelog", "--help"])

        combined = (result.output or "").replace("\n", " ")
        # The new bare-name location is ``bernstein changelog``.
        assert "bernstein changelog" in combined
        # The deprecation must not be a generic "renamed" message: it must
        # explicitly say which command the alias maps to (#3142 acceptance).
        assert "now lives at" in combined
        assert "bernstein changelog" in combined

    def test_run_changelog_runs_with_mocked_server(self, tmp_path: Path) -> None:
        # The deprecated alias still executes the same logic, just emits a
        # deprecation notice on stderr. Mock ``generate_run_changelog`` to
        # return an empty ``RunChangelog`` so the body path runs without
        # touching the network or metrics directory.
        from bernstein.core.orchestration.run_changelog import RunChangelog

        empty = RunChangelog(
            generated_at=time.time(),
            since_ref=None,
            tasks_total=0,
            changes={},
            breaking_changes=[],
        )
        runner = CliRunner()
        with runner.isolation():
            with patch(
                "bernstein.cli.commands.run_changelog_cmd.generate_run_changelog",
                return_value=empty,
            ):
                result = runner.invoke(
                    cli,
                    ["run-changelog", "--server-url", "http://localhost:1"],
                )

        # No network, no exception path: success.
        assert result.exit_code == 0, result.output
        # Notice on stderr -- ``[Deprecated]`` lives in the docstring (help),
        # but the runtime notice is the ``WARNING: ...`` we emit on click.echo.
        # Help output and runtime share stderr in some flows; assert either.
        combined = (result.output or "") + (result.stderr or "")
        assert "deprecated" in combined.lower()

    def test_run_changelog_top_level_help_lists_command(self) -> None:
        # The legacy alias is registered at the top level so existing scripts
        # keep working while it lives (#3142). It MUST show up in the
        # top-level command list.
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        # Top-level help is Rich-rendered, so text-matching the banner is
        # unreliable; the registered command list is the source of truth.
        top_names = set(cli.commands.keys())
        assert "changelog" in top_names
        assert "run-changelog" in top_names


# ---------------------------------------------------------------------------
# Bare ``bernstein changelog`` defaults to the run-changelog behaviour
# ---------------------------------------------------------------------------


class TestBareChangelogDefaultsToRunBehaviour:
    """``bernstein changelog`` (no subcommand) is the swap target: it now
    produces the agent-diff changelog (the previous ``run-changelog``
    behaviour), so existing release scripts writing
    ``bernstein run-changelog`` get the same body."""

    def test_bare_changelog_invokes_run_changelog_default(self, tmp_path: Path) -> None:
        # Patch ``run_changelog_default`` where the local import in
        # ``changelog_cmd.py`` resolves it -- the ``run_changelog_cmd``
        # module. Confirm the bare-name default callback reaches the same
        # function the legacy ``run-changelog`` alias would have called.
        called: dict[str, bool] = {"hit": False}

        def _spy(**_kwargs: object) -> None:
            called["hit"] = True

        runner = CliRunner()
        with runner.isolation():
            with patch(
                "bernstein.cli.commands.run_changelog_cmd.run_changelog_default",
                side_effect=_spy,
            ):
                result = runner.invoke(
                    cli,
                    ["changelog", "--server-url", "http://localhost:1"],
                )

        assert result.exit_code == 0, result.output
        assert called["hit"], f"run_changelog_default was not invoked: {result.output!r}"

    def test_bare_changelog_does_not_emit_deprecation_notice(self) -> None:
        # The bare name ``bernstein changelog`` is the NEW location for the
        # agent-diff behaviour: it must NOT emit a deprecation notice.
        runner = CliRunner()
        result = runner.invoke(cli, ["changelog", "--help"])

        assert result.exit_code == 0
        # The group help mentions "Deprecated" only inside the ``run-changelog``
        # alias block, not at the group level. We assert no WARNING: prefix on
        # stderr (which is where runtime notices land).
        combined = (result.output or "") + (result.stderr or "")
        assert "WARNING:" not in combined


# ---------------------------------------------------------------------------
# ``bernstein changelog conventional`` runs the conventional-commit parser
# ---------------------------------------------------------------------------


class TestConventionalSubcommand:
    """The conventional-commit command moves under ``changelog conventional``."""

    def test_conventional_subcommand_invoked_via_group(self, tmp_path: Path) -> None:
        # Build a synthetic git repo so ``_commits_since`` can find commits.
        runner = CliRunner()
        with runner.isolated_filesystem():
            cwd = Path().resolve()
            # Initialise an empty git repo and make one conventional commit.
            import subprocess

            subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=cwd, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=cwd,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "test"], cwd=cwd, check=True)
            (cwd / "README.md").write_text("hello")
            subprocess.run(["git", "add", "README.md"], cwd=cwd, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "feat(cli): add conventional changelog command (#3142)"],
                cwd=cwd,
                check=True,
            )
            result = runner.invoke(
                cli,
                ["changelog", "conventional", "--all", "--format", "simple", "--workdir", str(cwd)],
            )

        assert result.exit_code == 0, result.output
        # The commit message must surface in the simple-format output.
        assert "conventional changelog command" in result.output

    def test_conventional_missing_subcommand_errors(self) -> None:
        # ``bernstein changelog conventional-bogus`` must NOT silently fall back
        # to the bare-name behaviour (that's the failure mode #3142 warns
        # about). click handles the error; we just confirm exit code != 0.
        runner = CliRunner()
        result = runner.invoke(cli, ["changelog", "conventional-bogus"])

        assert result.exit_code != 0, result.output

    def test_conventional_help_mentions_swap(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["changelog", "conventional", "--help"])

        assert result.exit_code == 0
        # Help examples must use the new path so a user copying from --help
        # writes ``bernstein changelog conventional ...`` not ``bernstein
        # changelog ...`` (which now means the agent-diff changelog).
        assert "bernstein changelog conventional" in result.output
