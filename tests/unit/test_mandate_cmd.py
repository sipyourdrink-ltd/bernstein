"""CLI tests for ``bernstein mandate emit|verify|revoke`` (#2306)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.mandate_cmd import mandate_group
from bernstein.core.protocols.payments.mandates import CartMandate, IntentMandate
from bernstein.core.security.audit import load_or_create_audit_key


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with an isolated, repo-local audit key."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_inputs(project: Path) -> tuple[Path, Path, Path, str]:
    key = load_or_create_audit_key(project / "audit.key")
    intent = IntentMandate(
        task_id="task-1",
        allowed_tool_calls=("search", "buy"),
        spend_cap_usd=10.0,
    ).sign(key)
    cart = CartMandate(
        intent_hash=intent.mandate_hash(),
        tool_calls=("buy",),
        amount_usd=5.0,
    ).sign(key)
    settlement = {
        "challenge_hash": "sha256:" + "a" * 64,
        "payment_ref": "pref-1",
        "retried_request_hash": "sha256:" + "b" * 64,
        "amount_usd": 5.0,
    }
    i_path = project / "intent.json"
    c_path = project / "cart.json"
    s_path = project / "settlement.json"
    i_path.write_text(json.dumps(intent.to_dict()), encoding="utf-8")
    c_path.write_text(json.dumps(cart.to_dict()), encoding="utf-8")
    s_path.write_text(json.dumps(settlement), encoding="utf-8")
    return i_path, c_path, s_path, cart.mandate_hash()


def test_emit_then_verify_roundtrip(project: Path) -> None:
    i_path, c_path, s_path, mandate_hash = _write_inputs(project)
    runner = CliRunner()

    emit = runner.invoke(
        mandate_group,
        [
            "emit",
            "--intent",
            str(i_path),
            "--cart",
            str(c_path),
            "--settlement",
            str(s_path),
            "--no-sign",
            "--workdir",
            str(project),
        ],
    )
    assert emit.exit_code == 0, emit.output
    assert "consent receipt anchored" in emit.output

    verify = runner.invoke(
        mandate_group,
        ["verify", mandate_hash, "--intent", str(i_path), "--cart", str(c_path), "--workdir", str(project)],
    )
    assert verify.exit_code == 0, verify.output
    assert "authorized by the recorded intent" in verify.output


def test_revoke_then_verify_fails(project: Path) -> None:
    i_path, c_path, s_path, mandate_hash = _write_inputs(project)
    runner = CliRunner()
    runner.invoke(
        mandate_group,
        [
            "emit",
            "--intent",
            str(i_path),
            "--cart",
            str(c_path),
            "--settlement",
            str(s_path),
            "--no-sign",
            "--workdir",
            str(project),
        ],
    )
    revoke = runner.invoke(
        mandate_group,
        ["revoke", mandate_hash, "--reason", "budget change", "--workdir", str(project)],
    )
    assert revoke.exit_code == 0, revoke.output

    verify = runner.invoke(
        mandate_group,
        ["verify", mandate_hash, "--intent", str(i_path), "--cart", str(c_path), "--workdir", str(project)],
    )
    assert verify.exit_code == 2, verify.output
    assert "revoked" in verify.output
