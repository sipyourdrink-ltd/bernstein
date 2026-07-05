"""Tests for ``bernstein templates compress`` / ``restore`` (issue #2249).

The compression is operator-gated: it runs only through this explicit
command (confirmation required unless ``--yes``), prints only the
template token delta with a ledger reference for savings, and exits
non-zero when a role fails validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli

ORIGINAL_SYSTEM = """\
# You are a QA Engineer

## Your specialization
You design and run test plans with great care and attention to detail,
always reading the existing suites before writing anything new at all.

## Rules
- Run `uv run ruff check src/` before completing

## Current task
{{TASK_DESCRIPTION}}
"""

COMPRESSED_SYSTEM = """\
# You are a QA Engineer

## Your specialization
Test plans; read existing suites first.

## Rules
- Run `uv run ruff check src/` before completing

## Current task
{{TASK_DESCRIPTION}}
"""


def _write_role(workdir: Path, role: str = "qa") -> Path:
    role_dir = workdir / "templates" / "roles" / role
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "system_prompt.md").write_text(ORIGINAL_SYSTEM, encoding="utf-8")
    return role_dir


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Route the compress command's adapter call to a canned rewrite."""
    import bernstein.cli.commands.templates_cmd as mod
    import bernstein.core.tokens.template_compression as engine

    calls: list[str] = []

    def _fake(model: str, provider: str):
        def _call(prompt: str) -> str:
            calls.append(prompt)
            return COMPRESSED_SYSTEM

        return _call

    monkeypatch.setattr(mod, "_compress_llm_call", _fake)
    # Keep backups inside the test tree rather than the operator's home.
    monkeypatch.setattr(engine, "default_backup_root", lambda: tmp_path / "backups")
    return calls


class TestTemplatesCompressCmd:
    def test_compress_prints_ledger_only_savings_line(self, tmp_path: Path, fake_adapter: list[str]) -> None:
        role_dir = _write_role(tmp_path)
        result = CliRunner().invoke(
            cli,
            ["templates", "compress", "qa", "--workdir", str(tmp_path), "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "template reduced" in result.output
        assert "per-spawn savings will appear in the ledger" in result.output
        # No dollar or percentage claims at compression time.
        assert "$" not in result.output
        assert "%" not in result.output
        assert (role_dir / "system_prompt.md").read_text(encoding="utf-8") == COMPRESSED_SYSTEM
        assert (tmp_path / "templates.lock").is_file()

    def test_compress_requires_confirmation(self, tmp_path: Path, fake_adapter: list[str]) -> None:
        role_dir = _write_role(tmp_path)
        result = CliRunner().invoke(
            cli,
            ["templates", "compress", "qa", "--workdir", str(tmp_path)],
            input="n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Cancelled" in result.output
        assert (role_dir / "system_prompt.md").read_text(encoding="utf-8") == ORIGINAL_SYSTEM
        assert not fake_adapter

    def test_role_and_all_are_mutually_exclusive(self, tmp_path: Path, fake_adapter: list[str]) -> None:
        _write_role(tmp_path)
        result = CliRunner().invoke(
            cli,
            ["templates", "compress", "qa", "--all", "--workdir", str(tmp_path), "--yes"],
        )
        assert result.exit_code != 0
        assert "exactly one of ROLE or --all" in result.output

    def test_missing_role_and_all_fails(self, tmp_path: Path, fake_adapter: list[str]) -> None:
        _write_role(tmp_path)
        result = CliRunner().invoke(cli, ["templates", "compress", "--workdir", str(tmp_path), "--yes"])
        assert result.exit_code != 0

    def test_all_compresses_every_role_dir(self, tmp_path: Path, fake_adapter: list[str]) -> None:
        _write_role(tmp_path, "qa")
        qa2 = tmp_path / "templates" / "roles" / "backend"
        qa2.mkdir(parents=True)
        (qa2 / "system_prompt.md").write_text(ORIGINAL_SYSTEM, encoding="utf-8")
        result = CliRunner().invoke(
            cli,
            ["templates", "compress", "--all", "--workdir", str(tmp_path), "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert result.output.count("template reduced") == 2

    def test_unknown_role_exits_nonzero(self, tmp_path: Path, fake_adapter: list[str]) -> None:
        _write_role(tmp_path)
        result = CliRunner().invoke(
            cli,
            ["templates", "compress", "nope", "--workdir", str(tmp_path), "--yes"],
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_failed_validation_exits_nonzero_and_keeps_original(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bernstein.cli.commands.templates_cmd as mod

        role_dir = _write_role(tmp_path)
        bad = COMPRESSED_SYSTEM.replace("`uv run ruff check src/`", "the linter")
        monkeypatch.setattr(mod, "_compress_llm_call", lambda model, provider: lambda prompt: bad)
        result = CliRunner().invoke(
            cli,
            ["templates", "compress", "qa", "--workdir", str(tmp_path), "--yes"],
        )
        assert result.exit_code == 1
        assert "validators rejected" in result.output
        assert (role_dir / "system_prompt.md").read_text(encoding="utf-8") == ORIGINAL_SYSTEM


class TestTemplatesRestoreCmd:
    def test_restore_round_trips_byte_identically(self, tmp_path: Path, fake_adapter: list[str]) -> None:
        role_dir = _write_role(tmp_path)
        compress = CliRunner().invoke(
            cli,
            ["templates", "compress", "qa", "--workdir", str(tmp_path), "--yes"],
        )
        assert compress.exit_code == 0, compress.output

        restore = CliRunner().invoke(cli, ["templates", "restore", "qa", "--workdir", str(tmp_path)])
        assert restore.exit_code == 0, restore.output
        assert "byte-identically" in restore.output
        assert (role_dir / "system_prompt.md").read_text(encoding="utf-8") == ORIGINAL_SYSTEM

    def test_restore_without_compression_fails_cleanly(self, tmp_path: Path) -> None:
        _write_role(tmp_path)
        result = CliRunner().invoke(cli, ["templates", "restore", "qa", "--workdir", str(tmp_path)])
        assert result.exit_code != 0
        assert "no receipted compression" in result.output

    def test_subcommands_registered_on_templates_group(self) -> None:
        result = CliRunner().invoke(cli, ["templates", "--help"])
        assert result.exit_code == 0, result.output
        assert "compress" in result.output
        assert "restore" in result.output
