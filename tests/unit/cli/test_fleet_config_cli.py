"""CLI tests for the fleet config plane (#2550): var / conn / ctx.

The command surfaces are exercised end-to-end through Click's CliRunner
against an isolated ``.sdd`` under ``tmp_path``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.conn_cmd import conn_group
from bernstein.cli.commands.ctx_cmd import ctx_group
from bernstein.cli.commands.var_cmd import var_group


@pytest.fixture(autouse=True)
def _isolated_audit_key(tmp_path: Path) -> None:
    """Point the audit HMAC key at an isolated path so tests never touch the
    developer's real key."""
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"cli-fleet-test-key-32-bytes-pad!!")
    key_path.chmod(0o600)
    prev = os.environ.get("BERNSTEIN_AUDIT_KEY_PATH")
    os.environ["BERNSTEIN_AUDIT_KEY_PATH"] = str(key_path)
    yield
    if prev is None:
        os.environ.pop("BERNSTEIN_AUDIT_KEY_PATH", None)
    else:
        os.environ["BERNSTEIN_AUDIT_KEY_PATH"] = prev


def _workdir(tmp_path: Path) -> str:
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return str(tmp_path)


def test_var_set_get_list_history(tmp_path: Path) -> None:
    runner = CliRunner()
    wd = _workdir(tmp_path)

    r = runner.invoke(var_group, ["set", "threshold", "5", "-w", wd])
    assert r.exit_code == 0, r.output
    assert "set threshold" in r.output

    r = runner.invoke(var_group, ["set", "threshold", "9", "-w", wd])
    assert r.exit_code == 0, r.output

    r = runner.invoke(var_group, ["get", "threshold", "-w", wd])
    assert r.exit_code == 0, r.output
    assert "9" in r.output

    r = runner.invoke(var_group, ["list", "-w", wd])
    assert r.exit_code == 0, r.output
    assert "threshold" in r.output

    r = runner.invoke(var_group, ["history", "threshold", "--json", "-w", wd])
    assert r.exit_code == 0, r.output
    assert '"chain_position": 0' in r.output
    assert '"chain_position": 1' in r.output


def test_var_get_missing_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    wd = _workdir(tmp_path)
    r = runner.invoke(var_group, ["get", "nope", "-w", wd])
    assert r.exit_code == 1


def test_conn_create_list_rotate_audit(tmp_path: Path) -> None:
    runner = CliRunner()
    wd = _workdir(tmp_path)

    r = runner.invoke(
        conn_group,
        ["create", "prod-github", "--secret", "gh_pat", "--scope", "repo:read", "-w", wd],
    )
    assert r.exit_code == 0, r.output
    assert "created prod-github" in r.output

    r = runner.invoke(conn_group, ["list", "-w", wd])
    assert r.exit_code == 0, r.output
    assert "prod-github -> secret gh_pat" in r.output

    r = runner.invoke(conn_group, ["rotate", "prod-github", "--secret", "gh_pat_v2", "-w", wd])
    assert r.exit_code == 0, r.output
    assert "v2" in r.output

    # No resolutions yet -> audit is empty but exits cleanly.
    r = runner.invoke(conn_group, ["audit", "-w", wd])
    assert r.exit_code == 0, r.output


def test_ctx_create_use_show_list(tmp_path: Path) -> None:
    runner = CliRunner()
    wd = _workdir(tmp_path)

    r = runner.invoke(
        ctx_group,
        ["create", "staging", "--server-url", "https://staging", "--set", "budget=42", "-w", wd],
    )
    assert r.exit_code == 0, r.output

    r = runner.invoke(ctx_group, ["use", "staging", "-w", wd])
    assert r.exit_code == 0, r.output
    assert "using context staging" in r.output

    r = runner.invoke(ctx_group, ["show", "-w", wd])
    assert r.exit_code == 0, r.output
    assert "staging" in r.output
    assert "https://staging" in r.output

    r = runner.invoke(ctx_group, ["list", "-w", wd])
    assert r.exit_code == 0, r.output
    assert "staging" in r.output


def test_ctx_use_missing_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    wd = _workdir(tmp_path)
    r = runner.invoke(ctx_group, ["use", "ghost", "-w", wd])
    assert r.exit_code == 1


def test_audit_verify_fails_after_variable_tamper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Acceptance criterion #1: mutating a historical variable write flips
    # ``bernstein audit verify`` to a non-zero exit.
    from bernstein.cli.commands import audit_cmd
    from bernstein.cli.commands.audit_cmd import audit_group

    runner = CliRunner()
    wd = _workdir(tmp_path)
    runner.invoke(var_group, ["set", "thr", "5", "-w", wd])
    runner.invoke(var_group, ["set", "thr", "9", "-w", wd])

    audit_dir = Path(wd) / ".sdd" / "audit"
    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", audit_dir)
    monkeypatch.setattr(audit_cmd, "MERKLE_DIR", audit_dir / "merkle")

    # Baseline: the untampered chain verifies.
    ok = runner.invoke(audit_group, ["verify", "--hmac-only"])
    assert ok.exit_code == 0, ok.output

    # Tamper with a persisted historical write.
    day_file = next(audit_dir.glob("*.jsonl"))
    raw = day_file.read_text(encoding="utf-8")
    day_file.write_text(raw.replace('"new_value_hash": "sha256:', '"new_value_hash": "sha256:0'), encoding="utf-8")

    bad = runner.invoke(audit_group, ["verify", "--hmac-only"])
    assert bad.exit_code == 1
