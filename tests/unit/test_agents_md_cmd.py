"""End-to-end tests for ``bernstein agents-md`` Click subcommands.

Mirrors the ``test_lineage_export.py`` pattern: ``CliRunner.invoke`` against
``tmp_path`` fixture repos, then assert exit codes + on-disk files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.agents_md_cmd import agents_md_cmd

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_repo(tmp_path: Path) -> Path:
    """Minimal but complete demo repo for CLI smoke tests."""
    (tmp_path / "src" / "bernstein" / "core").mkdir(parents=True)
    (tmp_path / "src" / "bernstein" / "core" / "__init__.py").write_text("")
    (tmp_path / "src" / "bernstein" / "core" / "models.py").write_text('"""Domain models."""\n')
    (tmp_path / "templates" / "roles" / "backend").mkdir(parents=True)
    (tmp_path / "templates" / "roles" / "backend" / "system_prompt.md").write_text("# Backend\n")
    (tmp_path / "README.md").write_text("# Demo\n\nDemo project for CLI tests.\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0'\n")
    return tmp_path


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


class TestGenerateSubcommand:
    def test_canonical_prints_h1(self, demo_repo: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            agents_md_cmd,
            ["generate", "--workdir", str(demo_repo), "--repo-name", "demo"],
        )
        assert result.exit_code == 0, result.output
        assert "# demo - AGENTS.md" in result.output

    def test_cursor_target_prints_separator_per_file(self, demo_repo: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            agents_md_cmd,
            [
                "generate",
                "--workdir",
                str(demo_repo),
                "--target",
                "cursor",
                "--repo-name",
                "demo",
            ],
        )
        assert result.exit_code == 0, result.output
        # Multi-file target uses ``--- <relpath> ---`` separators.
        assert "--- .cursor/rules/" in result.output


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWriteSubcommand:
    def test_writes_canonical_to_repo(self, demo_repo: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            agents_md_cmd,
            ["write", "--workdir", str(demo_repo), "--repo-name", "demo"],
        )
        assert result.exit_code == 0, result.output
        agents_md = demo_repo / "AGENTS.md"
        assert agents_md.is_file()
        assert "# demo - AGENTS.md" in agents_md.read_text()

    def test_dry_run_does_not_touch_disk(self, demo_repo: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            agents_md_cmd,
            [
                "write",
                "--workdir",
                str(demo_repo),
                "--repo-name",
                "demo",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "[dry-run]" in result.output
        assert not (demo_repo / "AGENTS.md").exists()

    def test_cursor_target_writes_per_section_mdc(self, demo_repo: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            agents_md_cmd,
            [
                "write",
                "--workdir",
                str(demo_repo),
                "--target",
                "cursor",
                "--repo-name",
                "demo",
            ],
        )
        assert result.exit_code == 0, result.output
        rules_dir = demo_repo / ".cursor" / "rules"
        assert rules_dir.is_dir()
        # At least one .mdc file exists.
        assert any(p.suffix == ".mdc" for p in rules_dir.iterdir())


# ---------------------------------------------------------------------------
# sync - the killer command
# ---------------------------------------------------------------------------


class TestSyncSubcommand:
    def test_sync_writes_all_five_targets(self, demo_repo: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            agents_md_cmd,
            ["sync", "--workdir", str(demo_repo), "--repo-name", "demo"],
        )
        assert result.exit_code == 0, result.output
        assert (demo_repo / "AGENTS.md").is_file()
        assert (demo_repo / "CLAUDE.md").is_file()
        assert (demo_repo / "CONVENTIONS.md").is_file()
        assert (demo_repo / ".aider.conf.yml").is_file()
        assert (demo_repo / ".goosehints").is_file()
        assert (demo_repo / ".cursor" / "rules").is_dir()


# ---------------------------------------------------------------------------
# verify - CI gate
# ---------------------------------------------------------------------------


class TestVerifySubcommand:
    def test_verify_passes_immediately_after_sync(self, demo_repo: Path) -> None:
        runner = CliRunner()
        sync = runner.invoke(
            agents_md_cmd,
            ["sync", "--workdir", str(demo_repo), "--repo-name", "demo"],
        )
        assert sync.exit_code == 0, sync.output
        verify = runner.invoke(
            agents_md_cmd,
            ["verify", "--workdir", str(demo_repo), "--repo-name", "demo"],
        )
        assert verify.exit_code == 0, verify.output

    def test_verify_fails_when_canonical_missing(self, demo_repo: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            agents_md_cmd,
            [
                "verify",
                "--workdir",
                str(demo_repo),
                "--target",
                "canonical",
                "--repo-name",
                "demo",
            ],
        )
        assert result.exit_code == 1
        assert "MISSING" in result.output

    def test_verify_fails_when_drift_introduced(self, demo_repo: Path) -> None:
        runner = CliRunner()
        runner.invoke(
            agents_md_cmd,
            ["sync", "--workdir", str(demo_repo), "--repo-name", "demo"],
        )
        # Hand-edit AGENTS.md to introduce drift.
        agents = demo_repo / "AGENTS.md"
        agents.write_text(agents.read_text() + "\n<!-- manual edit -->\n")
        result = runner.invoke(
            agents_md_cmd,
            ["verify", "--workdir", str(demo_repo), "--repo-name", "demo"],
        )
        assert result.exit_code == 1
        assert "DRIFT" in result.output


# ---------------------------------------------------------------------------
# diff - informational, exit 0 even with drift
# ---------------------------------------------------------------------------


class TestDiffSubcommand:
    def test_no_diff_after_sync(self, demo_repo: Path) -> None:
        runner = CliRunner()
        runner.invoke(
            agents_md_cmd,
            ["sync", "--workdir", str(demo_repo), "--repo-name", "demo"],
        )
        result = runner.invoke(
            agents_md_cmd,
            ["diff", "--workdir", str(demo_repo), "--repo-name", "demo"],
        )
        assert result.exit_code == 0, result.output
        assert "No drift" in result.output

    def test_unified_diff_emitted_on_drift(self, demo_repo: Path) -> None:
        runner = CliRunner()
        runner.invoke(
            agents_md_cmd,
            ["sync", "--workdir", str(demo_repo), "--repo-name", "demo"],
        )
        agents = demo_repo / "AGENTS.md"
        agents.write_text(agents.read_text() + "\n<!-- manual edit -->\n")
        result = runner.invoke(
            agents_md_cmd,
            ["diff", "--workdir", str(demo_repo), "--repo-name", "demo"],
        )
        assert result.exit_code == 0
        # Unified-diff fence and our manual edit signature should both appear.
        assert "@@" in result.output
        assert "manual edit" in result.output


# ---------------------------------------------------------------------------
# default-branch resolution failure (issue #4578) - exit code 3
# ---------------------------------------------------------------------------


class TestUnresolvedDefaultBranch:
    def test_sync_fails_with_clear_error_when_default_branch_unresolvable(self, tmp_path: Path) -> None:
        """A feature-branch checkout with no origin must refuse, not write HEAD.

        Regression for #4578: this used to write the checked-out branch name
        into every mirror. The CLI contract now is a non-zero exit with a
        message naming the escape hatches.
        """
        import subprocess

        repo = tmp_path / "noorigin"
        repo.mkdir()
        (repo / "README.md").write_text("x\n")
        subprocess.run(["git", "init", "-q", "-b", "feature/x"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        runner = CliRunner()
        result = runner.invoke(
            agents_md_cmd,
            ["sync", "--workdir", str(repo), "--repo-name", "demo"],
        )
        assert result.exit_code == 3
        assert "--default-branch" in result.output
        # Nothing was written: refusing beats corrupting.
        assert not (repo / "AGENTS.md").exists()

    def test_default_branch_option_resolves_the_failure(self, tmp_path: Path) -> None:
        """The escape hatch the error message points at actually works."""
        import subprocess

        repo = tmp_path / "pinned"
        repo.mkdir()
        (repo / "README.md").write_text("x\n")
        subprocess.run(["git", "init", "-q", "-b", "feature/x"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        runner = CliRunner()
        result = runner.invoke(
            agents_md_cmd,
            [
                "sync",
                "--workdir",
                str(repo),
                "--repo-name",
                "demo",
                "--default-branch",
                "trunk",
            ],
        )
        assert result.exit_code == 0, result.output
        agents = (repo / "AGENTS.md").read_text()
        assert "Default branch: `trunk`." in agents
