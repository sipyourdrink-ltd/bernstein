"""Tests for the ``bernstein eval --reliability K`` alias (issue #2933).

The pass^k reliability floor shipped on the bench substrate in #3384
(``bench run --reliability K``, signed ReliabilityReceipt,
``reliability-verify`` / ``reliability-check``).  The eval group flag is a
thin alias over that implementation, so these tests pin the aliasing
properties rather than re-testing the floor itself:

  * the flag routes into the exact bench reliability code path
  * the emitted receipt is the same signed artefact ``bench run`` emits
  * the receipt verifies with the shipped ``bench reliability-verify``
  * the flag cannot be combined with an eval subcommand
  * the help text points at the bench verify/check verbs
  * the group's no-flag behaviour is unchanged
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.eval_benchmark_cmd import eval_group
from bernstein.eval.bench.bench_cli import bench_group

# Wall-clock-derived fields: ``emitted_at`` is stamped at emit time, and
# ``receipt_hash`` / ``signature`` are computed over it.  Everything else in
# the receipt must be byte-identical between the two spellings.
_VOLATILE_FIELDS = ("emitted_at", "receipt_hash", "signature")


def _load_receipt_without_volatile_fields(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    for field in _VOLATILE_FIELDS:
        raw.pop(field)
    return raw


# ---------------------------------------------------------------------------
# Routing: the alias delegates into the bench implementation
# ---------------------------------------------------------------------------


def test_eval_reliability_flag_routes_to_the_bench_reliability_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import bernstein.eval.bench.bench_cli as bench_cli

    calls: list[dict[str, Any]] = []

    def _spy(suite_obj: Any, scheduler: str, k: int, out_path: Path, stub_signer: bool) -> None:
        calls.append(
            {
                "suite_version": suite_obj.version,
                "scheduler": scheduler,
                "k": k,
                "out_path": out_path,
                "stub_signer": stub_signer,
            }
        )

    monkeypatch.setattr(bench_cli, "_run_reliability", _spy)

    out = tmp_path / "receipt.json"
    result = CliRunner().invoke(
        eval_group,
        ["--reliability", "3", "--suite", "golden-v1", "--out", str(out), "--stub-signer"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "suite_version": "golden-v1",
            "scheduler": "default",
            "k": 3,
            "out_path": out,
            "stub_signer": True,
        }
    ]


# ---------------------------------------------------------------------------
# Artefact identity: same signed receipt, same verification story
# ---------------------------------------------------------------------------


def test_eval_reliability_produces_the_same_signed_receipt_as_bench_run(tmp_path: Path) -> None:
    bench_out = tmp_path / "bench_receipt.json"
    eval_out = tmp_path / "eval_receipt.json"

    bench_result = CliRunner().invoke(
        bench_group,
        ["run", "golden-v1", "--reliability", "2", "--stub-signer", "--out", str(bench_out)],
    )
    assert bench_result.exit_code == 0, bench_result.output

    eval_result = CliRunner().invoke(
        eval_group,
        ["--reliability", "2", "--suite", "golden-v1", "--stub-signer", "--out", str(eval_out)],
    )
    assert eval_result.exit_code == 0, eval_result.output
    assert "pass^2 floor" in eval_result.output

    bench_receipt = _load_receipt_without_volatile_fields(bench_out)
    eval_receipt = _load_receipt_without_volatile_fields(eval_out)
    assert eval_receipt == bench_receipt


def test_eval_reliability_receipt_passes_the_shipped_bench_verifier(tmp_path: Path) -> None:
    out = tmp_path / "receipt.json"
    run_result = CliRunner().invoke(
        eval_group,
        ["--reliability", "2", "--stub-signer", "--out", str(out)],
    )
    assert run_result.exit_code == 0, run_result.output

    verify_result = CliRunner().invoke(bench_group, ["reliability-verify", str(out)])
    assert verify_result.exit_code == 0, verify_result.output
    assert "MATCH" in verify_result.output


# ---------------------------------------------------------------------------
# Surface hygiene
# ---------------------------------------------------------------------------


def test_eval_reliability_rejects_combination_with_a_subcommand() -> None:
    result = CliRunner().invoke(eval_group, ["--reliability", "2", "list"])
    assert result.exit_code != 0
    assert "cannot be combined" in result.output


def test_eval_help_points_at_the_bench_reliability_verify_verbs() -> None:
    result = CliRunner().invoke(eval_group, ["--help"])
    assert result.exit_code == 0, result.output
    assert "--reliability" in result.output
    assert "reliability-verify" in result.output
    assert "reliability-check" in result.output


def test_eval_group_without_flag_still_shows_help_and_subcommands() -> None:
    result = CliRunner().invoke(eval_group, [])
    assert result.exit_code == 2
    assert "Evaluation harness" in result.output
    assert "run" in result.output
