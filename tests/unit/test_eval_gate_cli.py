"""CLI tests for the statistical eval gate surface (#2520).

Covers ``bernstein eval gate`` (emit a verdict receipt), ``bernstein eval
promotions`` (project the stage history from the chain), and ``bernstein eval
gate-verify`` (offline verification passthrough).
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.eval_benchmark_cmd import eval_group


def _write_result_set(path: Path, base_passes: int, n: int) -> None:
    path.write_text(
        json.dumps({f"t{i:03d}": (i < base_passes) for i in range(n)}),
        encoding="utf-8",
    )


def test_eval_gate_emits_verdict_receipt(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    _write_result_set(base, base_passes=4, n=16)
    _write_result_set(cand, base_passes=12, n=16)

    runner = CliRunner()
    result = runner.invoke(
        eval_group,
        [
            "gate",
            "--baseline",
            str(base),
            "--candidate",
            str(cand),
            "--workdir",
            str(tmp_path),
            "--audit-dir",
            str(tmp_path / ".sdd" / "audit"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["evidence"]["verdict"] == "significant_improvement"
    receipt_hash = payload["receipt_hash"]

    # gate-verify passes on the freshly sealed receipt.
    verify = runner.invoke(
        eval_group,
        ["gate-verify", receipt_hash, "--workdir", str(tmp_path), "--json"],
    )
    assert verify.exit_code == 0, verify.output
    assert json.loads(verify.output)["ok"] is True


def test_eval_gate_below_min_n_refuses_promotion(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    _write_result_set(base, base_passes=0, n=4)
    _write_result_set(cand, base_passes=4, n=4)

    runner = CliRunner()
    result = runner.invoke(
        eval_group,
        [
            "gate",
            "--baseline",
            str(base),
            "--candidate",
            str(cand),
            "--workdir",
            str(tmp_path),
            "--no-audit",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["evidence"]["verdict"] == "insufficient_evidence"
    assert payload["evidence"]["reason"] == "below_minimum_n"


def test_eval_promotions_projects_stage_history(tmp_path: Path) -> None:
    runner = CliRunner()
    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    _write_result_set(base, base_passes=4, n=16)
    _write_result_set(cand, base_passes=12, n=16)

    # Seal two significant improvements at distinct timestamps.
    for ts in ("1", "2"):
        res = runner.invoke(
            eval_group,
            [
                "gate",
                "--baseline",
                str(base),
                "--candidate",
                str(cand),
                "--candidate-id",
                "cfg",
                "--workdir",
                str(tmp_path),
                "--no-audit",
                "--timestamp",
                ts,
                "--json",
            ],
        )
        assert res.exit_code == 0, res.output

    proj = runner.invoke(
        eval_group,
        ["promotions", "--workdir", str(tmp_path), "--candidate-id", "cfg", "--json"],
    )
    assert proj.exit_code == 0, proj.output
    payload = json.loads(proj.output)
    assert payload["final_stage"] == "canary"
    assert payload["stage_at_prefix"] == ["shadow", "canary"]
