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


def test_eval_gate_verify_symlinked_store_refused_no_traceback(tmp_path: Path) -> None:
    # A genuine receipt sealed in another workdir, planted in an outside
    # directory that the victim's symlinked gate store points at: the refusal
    # must be a named verification failure with a nonzero exit, never a
    # traceback and never a verdict read from the outside file.
    runner = CliRunner()
    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    _write_result_set(base, base_passes=4, n=16)
    _write_result_set(cand, base_passes=12, n=16)
    elsewhere = tmp_path / "elsewhere"
    sealed = runner.invoke(
        eval_group,
        [
            "gate",
            "--baseline",
            str(base),
            "--candidate",
            str(cand),
            "--workdir",
            str(elsewhere),
            "--no-audit",
            "--json",
        ],
    )
    assert sealed.exit_code == 0, sealed.output
    receipt_hash = json.loads(sealed.output)["receipt_hash"]
    receipt_json = (elsewhere / ".sdd" / "eval" / "gate" / f"{receipt_hash}.json").read_text(encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f"{receipt_hash}.json").write_text(receipt_json, encoding="utf-8")

    victim = tmp_path / "victim"
    (victim / ".sdd" / "eval").mkdir(parents=True)
    (victim / ".sdd" / "eval" / "gate").symlink_to(outside)

    result = runner.invoke(
        eval_group,
        ["gate-verify", receipt_hash, "--workdir", str(victim), "--json"],
    )
    assert result.exit_code == 1, f"exit={result.exit_code} exc={result.exception!r} out={result.output}"
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "no verdict receipt" in payload["reason"]
