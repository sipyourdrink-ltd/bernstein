"""Tests for the ``bernstein eval`` command group dark paths.

Covers the lighter-weight eval subcommands that do not require a running
orchestrator or live agents:

  * ``eval list``        - empty state + populated state + custom --state-dir
  * ``eval diff``        - stdout payload, --output file, winner selection
  * ``eval report``      - no-runs error exit
  * ``eval failures``    - no-runs error exit, empty-failures success
  * ``eval synth-list``  - disabled toggle + populated registry
  * ``eval synth-generate`` - invalid --params rejection, missing scenario
  * help surfaces        - flag presence (catches accidental rename)

Each test asserts on a concrete effect: exit code, emitted file content,
or specific stdout text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.eval_benchmark_cmd import eval_group


class EvalAdapterEntry(TypedDict):
    """One per-adapter row in a seeded YAML-eval run fixture."""

    adapter: str
    overall_score: float
    golden_pass_rate: float


def _seed_yaml_run(state_dir: Path, name: str, per_adapter: list[EvalAdapterEntry]) -> Path:
    """Write a minimal persisted YAML-eval run JSON under the runs dir."""
    runs_dir = state_dir / "eval" / "yaml_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"yaml_run_{name}.json"
    path.write_text(json.dumps({"per_adapter": per_adapter}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# eval list
# ---------------------------------------------------------------------------


def test_eval_list_empty_reports_no_runs(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(eval_group, ["list", "--state-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "No YAML eval runs found" in result.output


def test_eval_list_shows_run_paths_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the width so this test asserts ordering and nothing else. Left to the
    # ambient terminal its outcome depends on how deep pytest happened to root
    # tmp_path, which is a property of the runner rather than of the command.
    monkeypatch.setenv("COLUMNS", "200")
    _seed_yaml_run(tmp_path, "aaa", [{"adapter": "mock", "overall_score": 0.5, "golden_pass_rate": 1.0}])
    _seed_yaml_run(tmp_path, "zzz", [{"adapter": "mock", "overall_score": 0.6, "golden_pass_rate": 1.0}])

    runner = CliRunner()
    result = runner.invoke(eval_group, ["list", "--state-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    # Both files listed.
    assert "yaml_run_aaa.json" in result.output
    assert "yaml_run_zzz.json" in result.output
    # Newest-first ordering: reverse sort puts zzz before aaa.
    assert result.output.index("yaml_run_zzz.json") < result.output.index("yaml_run_aaa.json")


def test_eval_list_emits_paths_unwrapped_at_narrow_width(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run path survives a narrow console intact, so the list can be piped."""
    monkeypatch.setenv("COLUMNS", "80")
    # Nested far enough that the rendered path clears any plausible console
    # width regardless of where pytest rooted tmp_path.
    state_dir = tmp_path / "a_deliberately_long_state_directory_name_to_exceed_the_default_width"
    seeded = _seed_yaml_run(state_dir, "aaa", [{"adapter": "mock", "overall_score": 0.5, "golden_pass_rate": 1.0}])
    assert len(str(seeded)) > 80, "fixture must exceed the width it is meant to defeat"

    runner = CliRunner()
    result = runner.invoke(eval_group, ["list", "--state-dir", str(state_dir)])
    assert result.exit_code == 0, result.output
    emitted = [line.strip() for line in result.output.splitlines() if line.strip()]
    assert emitted == [str(seeded)]


def test_eval_list_preserves_square_brackets_in_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``[...]`` path segment is data, not Rich markup, and must survive."""
    monkeypatch.setenv("COLUMNS", "200")
    state_dir = tmp_path / "[bold]"
    seeded = _seed_yaml_run(state_dir, "aaa", [{"adapter": "mock", "overall_score": 0.5, "golden_pass_rate": 1.0}])

    runner = CliRunner()
    result = runner.invoke(eval_group, ["list", "--state-dir", str(state_dir)])
    assert result.exit_code == 0, result.output
    assert str(seeded) in result.output


# ---------------------------------------------------------------------------
# eval diff
# ---------------------------------------------------------------------------


def test_eval_diff_to_stdout_emits_json(tmp_path: Path) -> None:
    a = _seed_yaml_run(tmp_path, "a", [{"adapter": "mock", "overall_score": 0.4, "golden_pass_rate": 0.8}])
    b = _seed_yaml_run(tmp_path, "b", [{"adapter": "mock", "overall_score": 0.9, "golden_pass_rate": 1.0}])

    runner = CliRunner()
    result = runner.invoke(eval_group, ["diff", str(a), str(b)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # B scored higher -> winner is "b".
    assert payload["winner"] == "b"
    assert payload["entries"][0]["adapter"] == "mock"
    assert payload["entries"][0]["overall_delta"] == pytest.approx(0.5, abs=1e-4)


def test_eval_diff_writes_output_file(tmp_path: Path) -> None:
    a = _seed_yaml_run(tmp_path, "a", [{"adapter": "mock", "overall_score": 0.9, "golden_pass_rate": 1.0}])
    b = _seed_yaml_run(tmp_path, "b", [{"adapter": "mock", "overall_score": 0.2, "golden_pass_rate": 0.5}])
    out = tmp_path / "diff.json"

    runner = CliRunner()
    result = runner.invoke(eval_group, ["diff", str(a), str(b), "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    written = json.loads(out.read_text())
    # A scored higher -> winner is "a".
    assert written["winner"] == "a"
    # The stdout note announces the winner.
    assert "winner=a" in result.output


def test_eval_diff_tie_within_tolerance(tmp_path: Path) -> None:
    a = _seed_yaml_run(tmp_path, "a", [{"adapter": "mock", "overall_score": 0.500, "golden_pass_rate": 1.0}])
    b = _seed_yaml_run(tmp_path, "b", [{"adapter": "mock", "overall_score": 0.505, "golden_pass_rate": 1.0}])

    runner = CliRunner()
    result = runner.invoke(eval_group, ["diff", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["winner"] == "tie"


def test_eval_diff_missing_file_is_usage_error(tmp_path: Path) -> None:
    a = _seed_yaml_run(tmp_path, "a", [{"adapter": "mock", "overall_score": 0.5, "golden_pass_rate": 1.0}])
    runner = CliRunner()
    result = runner.invoke(eval_group, ["diff", str(a), str(tmp_path / "nope.json")])
    # click.Path(exists=True) rejects the missing arg with exit code 2.
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# eval report / eval failures - no-runs error path
# ---------------------------------------------------------------------------


def test_eval_report_no_runs_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(eval_group, ["report"])
    assert result.exit_code == 1, result.output
    assert "No eval runs found" in result.output


def test_eval_failures_no_runs_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(eval_group, ["failures"])
    assert result.exit_code == 1, result.output
    assert "No eval runs found" in result.output


def test_eval_failures_empty_failures_reports_clean() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        runs_dir = Path(".sdd") / "eval" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "eval_run_001.json").write_text(json.dumps({"failures": []}))
        result = runner.invoke(eval_group, ["failures"])
    assert result.exit_code == 0, result.output
    assert "No failures in the most recent run" in result.output


def test_eval_failures_renders_taxonomy_counts() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        runs_dir = Path(".sdd") / "eval" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "eval_run_002.json").write_text(
            json.dumps(
                {
                    "failures": [
                        {"task": "t1", "taxonomy": "timeout", "details": "slow"},
                        {"task": "t2", "taxonomy": "timeout", "details": "slow again"},
                        {"task": "t3", "taxonomy": "wrong_output", "details": "bad diff"},
                    ]
                }
            )
        )
        result = runner.invoke(eval_group, ["failures"])
    assert result.exit_code == 0, result.output
    assert "Total failures:" in result.output
    assert "3" in result.output
    # The most common category (timeout, count 2) is reported.
    assert "timeout" in result.output


# ---------------------------------------------------------------------------
# eval synth-list
# ---------------------------------------------------------------------------


def test_eval_synth_list_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SYNTHETIC_EVAL_OFF", "1")
    runner = CliRunner()
    result = runner.invoke(eval_group, ["synth-list"])
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output.lower()


def test_eval_synth_list_shows_registered_scenarios(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_SYNTHETIC_EVAL_OFF", raising=False)
    runner = CliRunner()
    result = runner.invoke(eval_group, ["synth-list"])
    assert result.exit_code == 0, result.output
    # The registry table header is present and at least one scenario row.
    assert "Synthetic scenarios" in result.output
    assert "Severity" in result.output


# ---------------------------------------------------------------------------
# eval synth-generate - validation paths
# ---------------------------------------------------------------------------


def test_eval_synth_generate_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SYNTHETIC_EVAL_OFF", "1")
    runner = CliRunner()
    result = runner.invoke(eval_group, ["synth-generate", "--scenario", "large_diff"])
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output.lower()


def test_eval_synth_generate_bad_params_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_SYNTHETIC_EVAL_OFF", raising=False)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            eval_group,
            ["synth-generate", "--scenario", "large_diff", "--params", "not-a-kv-pair"],
        )
    assert result.exit_code == 2, result.output
    assert "Invalid --params" in result.output


def test_eval_synth_generate_unknown_scenario_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_SYNTHETIC_EVAL_OFF", raising=False)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            eval_group,
            ["synth-generate", "--scenario", "does_not_exist_xyz", "--count", "1"],
        )
    assert result.exit_code == 2, result.output
    assert "Generation failed" in result.output


def test_eval_synth_generate_requires_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    # The missing-required-arg check fires before the disabled-toggle branch,
    # but pin the env var off so the assertion holds under random ordering.
    monkeypatch.delenv("BERNSTEIN_SYNTHETIC_EVAL_OFF", raising=False)
    runner = CliRunner()
    result = runner.invoke(eval_group, ["synth-generate"])
    assert result.exit_code == 2, result.output
    assert "--scenario" in result.output


# ---------------------------------------------------------------------------
# help surfaces
# ---------------------------------------------------------------------------


def test_eval_group_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(eval_group, ["--help"])
    assert result.exit_code == 0, result.output
    for sub in ("list", "diff", "report", "failures", "run", "ab", "scenario"):
        assert sub in result.output, f"missing subcommand {sub} in eval --help"


def test_eval_run_help_lists_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(eval_group, ["run", "--help"])
    assert result.exit_code == 0, result.output
    assert "--tier" in result.output
    assert "--compare" in result.output
