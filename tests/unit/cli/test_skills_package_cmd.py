"""CLI tests for ``bernstein skills package`` (issue #2369)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.skills_package_cmd import package_group
from bernstein.core.skills.packaging import PACKAGED_SKILL_NAME, tree_content_hash
from bernstein.core.skills.provenance import read_install_receipt

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32


@pytest.fixture(autouse=True)
def _isolate_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))


def _workdir(tmp_path: Path) -> Path:
    workdir = tmp_path / "proj"
    workdir.mkdir(exist_ok=True)
    return workdir


def test_show_prints_bundled_asset_identity(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        package_group,
        ["show", "--workdir", str(_workdir(tmp_path))],
    )
    assert result.exit_code == 0, result.output
    assert PACKAGED_SKILL_NAME in result.output
    assert "sha256:" in result.output


def test_install_to_dest_writes_receipt_and_verifies(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    dest = tmp_path / "skills-dir" / PACKAGED_SKILL_NAME

    result = CliRunner().invoke(
        package_group,
        ["install", "--dest", str(dest), "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output
    assert (dest / "SKILL.md").is_file()

    skill_hash = tree_content_hash(dest)
    assert read_install_receipt(workdir, skill_hash) is not None

    verify = CliRunner().invoke(
        package_group,
        ["verify", "--dest", str(dest), "--workdir", str(workdir)],
    )
    assert verify.exit_code == 0, verify.output


def test_install_host_scope_project_targets_host_dir(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    result = CliRunner().invoke(
        package_group,
        ["install", "--host", "claude", "--scope", "project", "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output
    dest = workdir / ".claude" / "skills" / PACKAGED_SKILL_NAME
    assert (dest / "SKILL.md").is_file()


def test_verify_detects_tamper_with_exit_2(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    dest = tmp_path / "skills-dir" / PACKAGED_SKILL_NAME
    install = CliRunner().invoke(
        package_group,
        ["install", "--dest", str(dest), "--workdir", str(workdir)],
    )
    assert install.exit_code == 0, install.output

    (dest / "SKILL.md").write_text("tampered\n", encoding="utf-8")
    verify = CliRunner().invoke(
        package_group,
        ["verify", "--dest", str(dest), "--workdir", str(workdir)],
    )
    assert verify.exit_code == 2, verify.output


def test_verify_without_receipt_exits_1(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    dest = tmp_path / "unanchored"
    dest.mkdir()
    (dest / "SKILL.md").write_text("never installed\n", encoding="utf-8")
    result = CliRunner().invoke(
        package_group,
        ["verify", "--dest", str(dest), "--workdir", str(workdir)],
    )
    assert result.exit_code == 2, result.output


def test_install_record_only_anchors_plugin_checkout(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    checkout = tmp_path / "plugins" / "bernstein"
    (checkout / ".plugin").mkdir(parents=True)
    (checkout / ".plugin" / "plugin.json").write_text('{"name": "bernstein"}\n', encoding="utf-8")

    result = CliRunner().invoke(
        package_group,
        ["install", "--dest", str(checkout), "--record-only", "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output
    assert read_install_receipt(workdir, tree_content_hash(checkout)) is not None

    verify = CliRunner().invoke(
        package_group,
        ["verify", "--dest", str(checkout), "--workdir", str(workdir)],
    )
    assert verify.exit_code == 0, verify.output


def test_package_group_registered_under_skills() -> None:
    from bernstein.cli.main import cli

    skills = cli.commands["skills"]
    assert "package" in skills.commands  # type: ignore[attr-defined]
