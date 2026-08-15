"""Harness and CLI wiring for the clean-run attestation (#2930).

Covers: the attestation rides on ``TaskEvalResult``; a ``DIRTY`` verdict zeroes
the multiplicative ``Safety`` factor; scoring without an attestation is
unchanged; and ``bernstein eval clean-run verify`` re-verifies offline.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.security.network_isolation import Endpoint, NetworkPolicy
from bernstein.eval.clean_run import (
    CleanRunVerdict,
    build_clean_run_attestation,
    clean_run_attestation_path,
)
from bernstein.eval.golden import GoldenTask
from bernstein.eval.harness import EvalHarness

_KEY = b"k" * 32
_TS = 1_700_000_000
_HIDDEN_TEST_TOKEN = "expect-fib-10-equals-55-sentinel"


def _task() -> GoldenTask:
    return GoldenTask(
        id="golden-fib-001",
        tier="smoke",
        title="Implement fibonacci helper",
        description="Add a fibonacci helper to the math module.",
        completion_signals=[_HIDDEN_TEST_TOKEN],
    )


def _passing_telemetry() -> dict[str, object]:
    return {
        "task_id": "golden-fib-001",
        "duration_s": 10.0,
        "turns_used": 3,
        "tokens_input": 100,
        "tokens_output": 50,
        "cost_usd": 0.01,
        "tests_run": 2,
        "tests_passed": 2,
        "tests_failed": 0,
        "completion_signals_checked": 1,
        "completion_signals_passed": 1,
    }


def _attestation(tmp_path: Path, *, dirty: bool, key: bytes = _KEY):
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    journal = EventJournal("run-clean-1", tmp_path / ".sdd")
    journal.record("file_read", path="src/mathlib.py", content_window="def add(a, b): return a + b")
    if dirty:
        journal.record("tool_call", arguments={"query": _HIDDEN_TEST_TOKEN})
    events = load_events(journal.path).events
    return build_clean_run_attestation(
        task=_task(),
        journal_events=events,
        run_id="run-clean-1",
        worktree_root=worktree,
        network_policy=NetworkPolicy(allowed_endpoints=(Endpoint("127.0.0.1", 8052),)),
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=key,
        timestamp=_TS,
    )


# ---------------------------------------------------------------------------
# Harness: attach + Safety zeroing
# ---------------------------------------------------------------------------


def test_evaluate_task_attaches_the_attestation(tmp_path: Path) -> None:
    attestation = _attestation(tmp_path, dirty=False)
    harness = EvalHarness(tmp_path / ".sdd")
    result = harness.evaluate_task(_task(), _passing_telemetry(), clean_run_attestation=attestation)
    assert result.clean_run is attestation
    assert result.passed
    # This synthetic task has no golden source and no reference blobs: the
    # commitment must say so in its sealed coverage rather than pretending.
    assert attestation.contraband.reference_source_count == 0
    assert attestation.contraband.token_source_count >= 3


def test_dirty_attestation_zeroes_the_safety_factor(tmp_path: Path) -> None:
    attestation = _attestation(tmp_path, dirty=True)
    assert attestation.verdict == CleanRunVerdict.DIRTY.value
    harness = EvalHarness(tmp_path / ".sdd")
    task_result = harness.evaluate_task(_task(), _passing_telemetry(), clean_run_attestation=attestation)
    run_result = harness.compute_multiplicative_score([task_result])
    assert run_result.components["safety"] == 0.0
    assert run_result.score == 0.0
    assert run_result.multiplicative_components is not None
    assert run_result.multiplicative_components.safety == 0.0


def test_clean_attestation_leaves_safety_intact(tmp_path: Path) -> None:
    attestation = _attestation(tmp_path, dirty=False)
    harness = EvalHarness(tmp_path / ".sdd")
    task_result = harness.evaluate_task(_task(), _passing_telemetry(), clean_run_attestation=attestation)
    run_result = harness.compute_multiplicative_score([task_result])
    assert run_result.components["safety"] == 1.0
    assert run_result.score > 0.0


def test_scoring_without_an_attestation_is_unchanged(tmp_path: Path) -> None:
    harness = EvalHarness(tmp_path / ".sdd")
    with_none = harness.compute_multiplicative_score([harness.evaluate_task(_task(), _passing_telemetry())])
    control = EvalHarness(tmp_path / "control")
    baseline = control.compute_multiplicative_score([control.evaluate_task(_task(), _passing_telemetry())])
    assert with_none.components == baseline.components
    assert with_none.score == baseline.score


# ---------------------------------------------------------------------------
# CLI: bernstein eval clean-run verify
# ---------------------------------------------------------------------------


def _key_env(tmp_path: Path) -> dict[str, str]:
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    os.chmod(key_file, 0o600)
    return {"BERNSTEIN_AUDIT_KEY_PATH": str(key_file)}


def test_cli_verify_passes_on_a_sealed_attestation(tmp_path: Path) -> None:
    from bernstein.cli.commands.eval_benchmark_cmd import eval_group

    attestation = _attestation(tmp_path, dirty=False)
    runner = CliRunner()
    result = runner.invoke(
        eval_group,
        ["clean-run", "verify", attestation.attestation_hash, "--workdir", str(tmp_path), "--json"],
        env=_key_env(tmp_path),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["verdict"] == CleanRunVerdict.CLEAN.value


def test_cli_verify_fails_on_a_tampered_attestation(tmp_path: Path) -> None:
    from bernstein.cli.commands.eval_benchmark_cmd import eval_group

    attestation = _attestation(tmp_path, dirty=False)
    path = clean_run_attestation_path(tmp_path, attestation.attestation_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["journal_head"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        eval_group,
        ["clean-run", "verify", attestation.attestation_hash, "--workdir", str(tmp_path), "--json"],
        env=_key_env(tmp_path),
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["ok"] is False
