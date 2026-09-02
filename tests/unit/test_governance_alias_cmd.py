"""CLI tests for ``bernstein governance`` (deprecated alias) forwarding to ``govern``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import governance_alias_cmd
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.governance import RoleBindings, decide_access


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with an isolated, repo-local audit key."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _bindings(key: bytes) -> RoleBindings:
    return RoleBindings(
        group_to_role={"eng": "operator"},
        role_permissions={"operator": ("tasks:write",)},
    ).sign(key)


def _write_bindings(project: Path, bindings: RoleBindings) -> Path:
    path = project / "bindings.json"
    path.write_text(json.dumps(bindings.to_dict()), encoding="utf-8")
    return path


def test_governance_alias_emits_deprecation_warning(project: Path) -> None:
    """The deprecated ``governance`` alias prints a warning to stderr."""
    runner = CliRunner()
    result = runner.invoke(governance_alias_cmd, ["--help"])
    assert result.exit_code == 0
    assert "deprecated" in result.output.lower()


def test_governance_alias_forwards_verify(project: Path) -> None:
    """``bernstein governance verify ...`` forwards to ``govern verify ...``."""
    key = load_or_create_audit_key(project / "audit.key")
    bindings = _bindings(key)
    lineage_root = project / ".sdd" / "lineage"
    decide_access(
        run_id="run-1",
        lineage_root=lineage_root,
        hmac_key=key,
        subject="alice",
        idp_groups=("eng",),
        action="tasks:write",
        bindings=bindings,
        now=1000,
    )
    bindings_path = _write_bindings(project, bindings)

    runner = CliRunner()
    result = runner.invoke(
        governance_alias_cmd,
        ["verify", "run-1", "--bindings", str(bindings_path), "--workdir", str(project)],
    )
    assert result.exit_code == 0, result.output
    assert "deprecated" in result.output
    assert "OK" in result.output


def test_governance_alias_registers_all_subcommands() -> None:
    """Every ``govern`` subcommand must be mirrored on the deprecated alias."""
    from bernstein.cli.commands.governance_cmd import govern_group

    assert set(governance_alias_cmd.commands) == set(govern_group.commands)
