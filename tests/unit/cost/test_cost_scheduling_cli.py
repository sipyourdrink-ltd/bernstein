"""CLI surface for cost-aware scheduling: ``bernstein cost policy`` (#2354).

* ``preflight`` surfaces pool exhaustion before a run starts (AC5) and exits
  non-zero when a capped pool is (or would be) exhausted.
* ``verify`` re-checks a sealed dispatch receipt offline against the lineage
  spine, and fails on a tampered receipt.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.cost import cost_cmd
from bernstein.core.cost.scheduling.policy import CostCaps, DispatchCandidate, decide_dispatch
from bernstein.core.cost.scheduling.receipt import build_dispatch_receipt
from bernstein.core.cost.spend_ledger import LedgerEntry

_KEY = b"0" * 32


def _write_ledger(path: Path, *rows: tuple[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = 1_762_000_000.0
    lines = []
    for envelope, cost in rows:
        entry = LedgerEntry(
            ts=ts,
            ts_iso=datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds"),
            run_id="r1",
            task_id="t1",
            agent_id="a1",
            role="dev",
            feature_label="",
            model="sonnet",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=cost,
            quota_envelope=envelope,
        )
        lines.append(entry.to_json())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_config(path: Path, pools: dict[str, float]) -> None:
    import yaml

    path.write_text(yaml.safe_dump({"goal": "x", "cost_policy": {"pools": pools}}), encoding="utf-8")


def test_preflight_exits_nonzero_when_pool_exhausted(tmp_path: Path) -> None:
    ledger = tmp_path / ".sdd" / "cost" / "ledger.jsonl"
    _write_ledger(ledger, ("api", 9.5))
    config = tmp_path / "bernstein.yaml"
    _write_config(config, {"api": 10.0})

    runner = CliRunner()
    result = runner.invoke(
        cost_cmd,
        [
            "policy",
            "preflight",
            "--ledger",
            str(ledger),
            "--config",
            str(config),
            "--plan",
            "api=1.0",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert any(p["pool"] == "api" and p["exhausted"] for p in payload["pools"])


def test_preflight_exits_zero_with_headroom(tmp_path: Path) -> None:
    ledger = tmp_path / ".sdd" / "cost" / "ledger.jsonl"
    _write_ledger(ledger, ("api", 1.0))
    config = tmp_path / "bernstein.yaml"
    _write_config(config, {"api": 100.0})

    runner = CliRunner()
    result = runner.invoke(
        cost_cmd,
        ["policy", "preflight", "--ledger", str(ledger), "--config", str(config), "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True


def test_verify_roundtrip_and_tamper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI verify path resolves the audit key via the canonical loader; pin
    # it to the test key so the seal built here verifies cwd-independently.
    monkeypatch.setattr(
        "bernstein.core.security.audit.load_or_create_audit_key",
        lambda *_a, **_k: _KEY,
    )

    workdir = tmp_path / "proj"
    lineage_root = workdir / ".sdd" / "lineage"
    caps = CostCaps(per_run_usd=10.0)
    # A halting candidate (projected run spend 11.0 > cap 10.0) so forging
    # ``admit`` to True below is a real mutation, not a no-op.
    candidate = DispatchCandidate(
        task_id="t1", run_id="r1", model="sonnet", projected_cost_usd=11.0, day_key="2026-07-11", pool="api"
    )
    decision = decide_dispatch(
        candidate=candidate,
        entries=[],
        caps=caps,
        price_table_hash="sha256:pt",
    )
    assert decision.admit is False
    build_dispatch_receipt(
        decision=decision,
        workdir=workdir,
        lineage_root=lineage_root,
        hmac_key=_KEY,
        timestamp=1_762_000_001,
    )

    runner = CliRunner()
    ok = runner.invoke(cost_cmd, ["policy", "verify", decision.decision_hash, "--workdir", str(workdir), "--json"])
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output)["ok"] is True

    # Tamper the on-disk receipt: forge an admit.
    path = workdir / ".sdd" / "cost" / "dispatch" / f"{decision.decision_hash}.json"
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["admit"] = True
    path.write_text(json.dumps(forged), encoding="utf-8")

    bad = runner.invoke(cost_cmd, ["policy", "verify", decision.decision_hash, "--workdir", str(workdir), "--json"])
    assert bad.exit_code == 1, bad.output
    assert json.loads(bad.output)["ok"] is False
