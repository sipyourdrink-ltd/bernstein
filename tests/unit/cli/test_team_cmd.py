"""Tests for the ``bernstein team`` CLI group (issue #2248)."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.teams.drift import role_template_digest
from bernstein.core.teams.manifest import load_team_manifest

MANIFEST_TEMPLATE = """\
name = "crew"
version = "1.0.0"

[[roles]]
role = "backend"
response_profile = "terse"

[roles.model_policy]
model = "sonnet"

[role_template_digests]
"backend" = "{digest}"
"""


def _write_role(workdir: Path, role: str) -> Path:
    role_dir = workdir / "templates" / "roles" / role
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "system_prompt.md").write_text("# Specialist\n", encoding="utf-8")
    return role_dir


def _write_workdir(workdir: Path) -> Path:
    role_dir = _write_role(workdir, "backend")
    teams_dir = workdir / "templates" / "teams"
    teams_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = teams_dir / "crew.toml"
    manifest_path.write_text(
        MANIFEST_TEMPLATE.format(digest=role_template_digest(role_dir)),
        encoding="utf-8",
    )
    return manifest_path


class TestTeamList:
    def test_lists_local_and_builtin_manifests(self, tmp_path: Path) -> None:
        _write_workdir(tmp_path)
        result = CliRunner().invoke(cli, ["team", "list", "--workdir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "crew" in result.output
        assert "python" in result.output
        assert "typescript" in result.output

    def test_group_registered_on_main_cli(self) -> None:
        result = CliRunner().invoke(cli, ["team", "--help"])
        assert result.exit_code == 0, result.output
        assert "list" in result.output
        assert "show" in result.output
        assert "drift" in result.output


class TestTeamShow:
    def test_shows_digest_roles_and_policies(self, tmp_path: Path) -> None:
        manifest_path = _write_workdir(tmp_path)
        digest = load_team_manifest(manifest_path).digest()
        result = CliRunner().invoke(cli, ["team", "show", "crew", "--workdir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert digest in result.output
        assert "backend" in result.output
        assert "terse" in result.output
        assert "sonnet" in result.output

    def test_unknown_manifest_fails_cleanly(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["team", "show", "no-such-team", "--workdir", str(tmp_path)])
        assert result.exit_code != 0
        assert "not found" in result.output


class TestTeamDrift:
    def test_no_drift_when_pins_match(self, tmp_path: Path) -> None:
        _write_workdir(tmp_path)
        result = CliRunner().invoke(cli, ["team", "drift", "crew", "--workdir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "no drift" in result.output

    def test_one_byte_edit_is_reported(self, tmp_path: Path) -> None:
        """AC2 at the CLI surface: a one-byte template edit shows up."""
        _write_workdir(tmp_path)
        prompt = tmp_path / "templates" / "roles" / "backend" / "system_prompt.md"
        prompt.write_text(prompt.read_text(encoding="utf-8") + "x", encoding="utf-8")
        result = CliRunner().invoke(cli, ["team", "drift", "crew", "--workdir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "drift detected" in result.output
        assert "backend" in result.output

    def test_drift_without_name_checks_all_manifests(self, tmp_path: Path) -> None:
        _write_workdir(tmp_path)
        result = CliRunner().invoke(cli, ["team", "drift", "--workdir", str(tmp_path)])
        # The local crew manifest matches its pins; the built-in manifests
        # pin the bundled role templates, which this workdir shadows with
        # its own templates/ tree, so they report drift and exit 1.
        assert result.exit_code == 1, result.output
        assert "crew: no drift" in result.output
        assert "python" in result.output
