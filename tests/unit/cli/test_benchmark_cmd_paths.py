"""Tests for ``bernstein benchmark`` + remaining ``bernstein eval`` dark paths.

These cover the validation / error-exit / flag-parsing surface that does not
require running real agents or harnesses:

  * ``benchmark run``     - missing-dir error, invalid --tier
  * ``benchmark compare`` - missing-dir error, empty-dir error, invalid --mode
  * ``benchmark swe-bench`` - invalid --subset
  * ``eval ab``           - missing required flags, missing files, bad --scorer
  * ``eval scenario``     - missing SCENARIO_ID
  * ``eval calibration report`` - empty-log JSON shape, --output file, --bins range
  * help surfaces

The calibration-report happy path runs against an empty log (deterministic,
no network), asserting the JSON shape rather than mocking the computer.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.eval_benchmark_cmd import benchmark_group, eval_group

# ---------------------------------------------------------------------------
# benchmark run
# ---------------------------------------------------------------------------


def test_benchmark_run_missing_dir_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(benchmark_group, ["run", "--benchmarks-dir", "no-such-dir"])
    assert result.exit_code == 1, result.output
    assert "not found" in result.output.lower()


def test_benchmark_run_invalid_tier_is_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(benchmark_group, ["run", "--tier", "bogus"])
    assert result.exit_code == 2, result.output
    assert "bogus" in result.output or "Invalid value" in result.output


# ---------------------------------------------------------------------------
# benchmark compare
# ---------------------------------------------------------------------------


def test_benchmark_compare_missing_dir_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(benchmark_group, ["compare", "--tasks-dir", "no-such"])
    assert result.exit_code == 1, result.output
    assert "not found" in result.output.lower()


def test_benchmark_compare_empty_dir_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("emptytasks").mkdir()
        result = runner.invoke(benchmark_group, ["compare", "--tasks-dir", "emptytasks"])
    assert result.exit_code == 1, result.output
    assert "No benchmark tasks found" in result.output


def test_benchmark_compare_invalid_mode_is_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(benchmark_group, ["compare", "--mode", "bogus"])
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# benchmark swe-bench
# ---------------------------------------------------------------------------


def test_benchmark_swe_bench_invalid_subset_is_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(benchmark_group, ["swe-bench", "--subset", "mega"])
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# eval ab - argument validation
# ---------------------------------------------------------------------------


def test_eval_ab_missing_required_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(eval_group, ["ab"])
    assert result.exit_code == 2, result.output
    assert "--variant-a" in result.output


def test_eval_ab_missing_files_is_usage_error() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            eval_group,
            ["ab", "--variant-a", "a.yaml", "--variant-b", "b.yaml", "--tasks", "t.yaml"],
        )
    # click.Path(exists=True) rejects the missing files.
    assert result.exit_code == 2, result.output


def test_eval_ab_bad_scorer_is_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(
        eval_group,
        ["ab", "--scorer", "bogus", "--variant-a", "a", "--variant-b", "b", "--tasks", "t"],
    )
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# eval scenario - argument validation
# ---------------------------------------------------------------------------


def test_eval_scenario_requires_scenario_id() -> None:
    runner = CliRunner()
    result = runner.invoke(eval_group, ["scenario"])
    assert result.exit_code == 2, result.output
    assert "SCENARIO_ID" in result.output


# ---------------------------------------------------------------------------
# eval calibration report
# ---------------------------------------------------------------------------


def test_calibration_report_empty_log_emits_zero_decisions() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(eval_group, ["calibration", "report", "--log-path", "nope.jsonl"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["decisions"] == 0


def test_calibration_report_writes_output_file() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            eval_group,
            ["calibration", "report", "--log-path", "nope.jsonl", "--output", "out.json"],
        )
        assert result.exit_code == 0, result.output
        assert Path("out.json").exists()
        written = json.loads(Path("out.json").read_text())
        assert written["decisions"] == 0
    assert "wrote" in result.output


def test_calibration_report_rejects_zero_bins() -> None:
    runner = CliRunner()
    result = runner.invoke(eval_group, ["calibration", "report", "--bins", "0"])
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# help surfaces
# ---------------------------------------------------------------------------


def test_benchmark_group_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(benchmark_group, ["--help"])
    assert result.exit_code == 0, result.output
    for sub in ("run", "compare", "swe-bench", "simulate"):
        assert sub in result.output, f"missing {sub} in benchmark --help"


def test_eval_calibration_help_lists_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(eval_group, ["calibration", "report", "--help"])
    assert result.exit_code == 0, result.output
    assert "--since" in result.output
    assert "--bins" in result.output


# ---------------------------------------------------------------------------
# benchmark receipt emit - input validation
#
# emit turns a persisted run record into signed bytes. Anything it substitutes
# for missing input is sealed and then verifies clean, so a run record that is
# short of a field has to be refused rather than defaulted.
# ---------------------------------------------------------------------------


_GOOD_TASK = {
    "task_id": "smoke-001",
    "journal_head_hash": "sha256:" + "1" * 64,
    "events_content_hash": "sha256:" + "2" * 64,
    "model_id": "test-model",
    "config_fingerprint": "cfg-v1",
    "components": {
        "task_success": 1.0,
        "code_quality": 0.9,
        "efficiency": 0.8,
        "reliability": 1.0,
        "safety": 1.0,
    },
}
_GOOD_PER_TIER = {"smoke": 1.0, "standard": 0.0, "stretch": 0.0, "adversarial": 0.0}


def _write_run_record(root: Path, record: dict) -> None:
    runs_path = root / ".sdd" / "benchmarks" / "benchmark_runs.jsonl"
    runs_path.parent.mkdir(parents=True, exist_ok=True)
    runs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _emit(record: dict):
    runner = CliRunner()
    with runner.isolated_filesystem() as tmp:
        _write_run_record(Path(tmp), record)
        return runner.invoke(benchmark_group, ["receipt", "emit", "run-1", "--workdir", tmp])


def test_receipt_emit_rejects_task_missing_journal_head() -> None:
    task = {k: v for k, v in _GOOD_TASK.items() if k != "journal_head_hash"}
    result = _emit({"run_id": "run-1", "tasks": [task], "per_tier": _GOOD_PER_TIER})
    assert result.exit_code == 1, result.output
    assert "journal_head_hash" in result.output


def test_receipt_emit_rejects_placeholder_events_hash() -> None:
    task = {**_GOOD_TASK, "events_content_hash": "sha256:" + "0" * 64}
    result = _emit({"run_id": "run-1", "tasks": [task], "per_tier": _GOOD_PER_TIER})
    assert result.exit_code == 1, result.output
    assert "placeholder" in result.output


def test_receipt_emit_rejects_missing_score_component() -> None:
    components = {k: v for k, v in _GOOD_TASK["components"].items() if k != "efficiency"}
    task = {**_GOOD_TASK, "components": components}
    result = _emit({"run_id": "run-1", "tasks": [task], "per_tier": _GOOD_PER_TIER})
    assert result.exit_code == 1, result.output
    assert "efficiency" in result.output


def test_receipt_emit_rejects_missing_per_tier() -> None:
    result = _emit({"run_id": "run-1", "tasks": [_GOOD_TASK]})
    assert result.exit_code == 1, result.output
    assert "per_tier" in result.output


def test_receipt_emit_rejects_non_numeric_component() -> None:
    task = {**_GOOD_TASK, "components": {**_GOOD_TASK["components"], "safety": "high"}}
    result = _emit({"run_id": "run-1", "tasks": [task], "per_tier": _GOOD_PER_TIER})
    assert result.exit_code == 1, result.output
    assert "non-numeric" in result.output


def test_receipt_emit_accepts_a_complete_run_record() -> None:
    result = _emit({"run_id": "run-1", "tasks": [_GOOD_TASK], "per_tier": _GOOD_PER_TIER})
    assert result.exit_code == 0, result.output
    assert "Trajectory receipt emitted" in result.output
