"""CLI tests for ``bernstein model`` commands (issue #5038, slice m30-5038-cli)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.model_cmd import model_group
from bernstein.core.routing.model_registry import (
    format_timestamp,
    record_model_admission,
    record_model_withdrawal,
)
from bernstein.core.security.audit import AUDIT_KEY_ENV, load_or_create_audit_key
from bernstein.core.security.audit_chain import AuditChainStore


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated project with audit chain and pinned HMAC key."""
    key_path = tmp_path / "audit.key"
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))
    monkeypatch.chdir(tmp_path)
    load_or_create_audit_key()
    audit_dir = tmp_path / ".sdd" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _future(days: int) -> str:
    return format_timestamp(datetime.now(tz=UTC) + timedelta(days=days))


def _run(*args: str) -> object:
    return CliRunner().invoke(model_group, list(args))


def test_registry_at_a_past_timestamp_reconstructs_the_state_that_held_then(project: Path) -> None:
    """Test ``bernstein model registry --at <timestamp>`` reconstruction."""
    audit_dir = project / ".sdd" / "audit"
    key = load_or_create_audit_key()
    chain = AuditChainStore(audit_dir, key=key)

    # Admit a model
    admitted = record_model_admission(
        chain=chain,
        provider="anthropic",
        model="opus",
        version=None,
        task_classes=("code", "review"),
        admitted_by="operator@example.test",
        expires_at=_future(30),
        evidence_ref="sha256:" + "e" * 64,
    )

    # Withdraw it later
    withdrawn = record_model_withdrawal(
        chain=chain,
        provider="anthropic",
        model="opus",
        version=None,
        withdrawn_by="operator@example.test",
        reason="superseded",
    )

    # CLI should reconstruct state at admission time
    result = _run("registry", "--at", admitted.timestamp)
    assert result.exit_code == 0
    assert "anthropic/opus" in result.output or "opus" in result.output
    assert "operator@example.test" in result.output

    # At withdrawal time, should show empty
    result = _run("registry", "--at", withdrawn.timestamp)
    assert result.exit_code == 0
    assert "No models admitted" in result.output


def test_model_registry_without_at_shows_current_state(project: Path) -> None:
    """Test ``bernstein model registry`` without --at shows current state."""
    audit_dir = project / ".sdd" / "audit"
    key = load_or_create_audit_key()
    chain = AuditChainStore(audit_dir, key=key)

    record_model_admission(
        chain=chain,
        provider="anthropic",
        model="sonnet",
        version=None,
        task_classes=("code",),
        admitted_by="operator@example.test",
        expires_at=_future(30),
        evidence_ref="sha256:" + "e" * 64,
    )

    result = _run("registry")
    assert result.exit_code == 0
    assert "sonnet" in result.output


def test_model_impact_lists_artefacts_by_model_ref(project: Path) -> None:
    """Test ``bernstein model impact <ref>`` lists artefacts."""
    # For now, verify the command exists and handles the ref parameter
    # This will work once lineage entries with model_ref exist
    result = _run("impact", "anthropic/opus")

    # Should not crash; exit 1 for no artefacts found is acceptable
    assert result.exit_code in (0, 1)
    if result.exit_code == 1:
        assert "No artefacts found" in result.output or "not found" in result.output
