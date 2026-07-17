"""CLI tests for the fleet config plane (#2550): var / conn / ctx.

The command surfaces are exercised end-to-end through Click's CliRunner
against an isolated ``.sdd`` under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.conn_cmd import conn_group
from bernstein.cli.commands.ctx_cmd import ctx_group
from bernstein.cli.commands.var_cmd import var_group

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolated_audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the audit HMAC key at an isolated path so tests never touch the
    developer's real key."""
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"cli-fleet-test-key-32-bytes-pad!!")
    key_path.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    yield


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


def test_var_set_rejects_invalid_json_but_accepts_bare_word(tmp_path: Path) -> None:
    runner = CliRunner()
    wd = _workdir(tmp_path)
    # Looks like JSON (starts with '{') but is invalid -> rejected.
    bad = runner.invoke(var_group, ["set", "k", "{not json", "-w", wd])
    assert bad.exit_code != 0
    # A bare word is still accepted as a string.
    ok = runner.invoke(var_group, ["set", "k", "hello", "-w", wd])
    assert ok.exit_code == 0, ok.output
    got = runner.invoke(var_group, ["get", "k", "-w", wd])
    assert "hello" in got.output


def test_var_list_hides_values_by_default(tmp_path: Path) -> None:
    runner = CliRunner()
    wd = _workdir(tmp_path)
    runner.invoke(var_group, ["set", "endpoint", '"https://secret.example"', "-w", wd])
    default = runner.invoke(var_group, ["list", "-w", wd])
    assert "endpoint" in default.output
    assert "secret.example" not in default.output
    with_values = runner.invoke(var_group, ["list", "--values", "-w", wd])
    assert "secret.example" in with_values.output


def test_conn_audit_json_empty_is_valid_json(tmp_path: Path) -> None:
    runner = CliRunner()
    wd = _workdir(tmp_path)
    r = runner.invoke(conn_group, ["audit", "--json", "-w", wd])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "[]"


def test_ctx_create_adapter_default_and_invalid_name(tmp_path: Path) -> None:
    runner = CliRunner()
    wd = _workdir(tmp_path)
    ok = runner.invoke(
        ctx_group,
        ["create", "staging", "--adapter-default", "model=claude", "-w", wd],
    )
    assert ok.exit_code == 0, ok.output
    shown = runner.invoke(ctx_group, ["show", "staging", "--json", "-w", wd])
    assert '"model"' in shown.output and "claude" in shown.output
    # An invalid name is a clean parameter error, not a traceback.
    bad = runner.invoke(ctx_group, ["create", "../escape", "-w", wd])
    assert bad.exit_code != 0


def test_ctx_show_json_null_when_no_active(tmp_path: Path) -> None:
    runner = CliRunner()
    wd = _workdir(tmp_path)
    r = runner.invoke(ctx_group, ["show", "--json", "-w", wd])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "null"


def test_ctx_show_redacts_store_dsn(tmp_path: Path) -> None:
    runner = CliRunner()
    wd = _workdir(tmp_path)
    runner.invoke(
        ctx_group,
        ["create", "prod", "--store-dsn", "postgres://user:topsecret@host:5432/db", "-w", wd],
    )
    redacted = runner.invoke(ctx_group, ["show", "prod", "-w", wd])
    assert "topsecret" not in redacted.output
    assert "***" in redacted.output
    revealed = runner.invoke(ctx_group, ["show", "prod", "--reveal", "-w", wd])
    assert "topsecret" in revealed.output


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
