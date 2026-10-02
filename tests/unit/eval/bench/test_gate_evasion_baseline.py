"""Strict, content-addressed gate-evasion baseline checks (#6154)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.eval.bench import gate_evasion_baseline as baseline
from bernstein.eval.bench.bench_cli import bench_group
from bernstein.eval.bench.gate_evasion_suite import (
    GateEvasionCase,
    GateEvasionResult,
    score_gate_evasion,
)


def _measured(caught: tuple[str, ...] = ("a",), names: tuple[str, ...] = ("a", "b")) -> dict:
    entries = [
        {
            "case_class": name,
            "gate_that_must_flag": "lint",
            "expected_verdict": "fail",
            "actual_verdict": "fail" if name in caught else "pass",
            "caught": name in caught,
            "verdict_basis": "finding_signature" if name in caught else "gate_verdict",
        }
        for name in sorted(names)
    ]
    count = sum(c["caught"] for c in entries)
    return {
        "schema_version": 1,
        "suite_version": "gate-evasion-v1",
        "suite_hash": "a" * 64,
        "corpus_sha256": "b" * 64,
        "tools": {"pytest": "9.1.1", "ruff": "0.16.7", "vulture": "2.14"},
        "cases": entries,
        "total": len(entries),
        "caught_count": count,
        "catch_rate": count / len(entries),
    }


def _save(path: Path, measured: dict) -> None:
    payload = {**measured, "update_reason": "Reviewed intentional baseline measurement"}
    payload["content_sha256"] = baseline._digest(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_committed_baseline_integrity_and_corpus_identity() -> None:
    path = Path(".github/gate-evasion-baseline.json")
    data = baseline.read_baseline(path)
    assert data["total"] == len(data["cases"]) == 8
    assert data["corpus_sha256"] == baseline.corpus_bytes_digest()
    assert data["tools"] == {"pytest": "9.1.1", "ruff": "0.16.7", "vulture": "2.14"}


def test_equal_and_improved_catch_sets_pass(tmp_path: Path) -> None:
    original = _measured()
    path = tmp_path / "baseline.json"
    _save(path, original)
    baseline.compare_to_baseline(baseline.read_baseline(path), original)
    baseline.compare_to_baseline(baseline.read_baseline(path), _measured(("a", "b")))


def test_caught_to_missed_fails_even_when_total_is_equal_or_higher(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    _save(path, _measured(("a",), ("a", "b", "c")))
    reference = baseline.read_baseline(path)
    for caught in (("b",), ("b", "c")):
        with pytest.raises(ValueError, match=r"caught-to-missed regression: a \(lint\)"):
            baseline.compare_to_baseline(reference, _measured(caught, ("a", "b", "c")))


def test_rate_drop_and_changed_identity_fail(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    _save(path, _measured(("a", "b")))
    reference = baseline.read_baseline(path)
    with pytest.raises(ValueError, match="catch rate regressed"):
        baseline.compare_to_baseline(reference, _measured(()))
    for key, new_value in (
        ("suite_hash", "d" * 64),
        ("corpus_sha256", "e" * 64),
        ("tools", {"ruff": "old", "pytest": "9.1.1", "vulture": "2.14"}),
    ):
        changed = {**_measured(("a", "b")), key: new_value}
        with pytest.raises(ValueError, match=key):
            baseline.compare_to_baseline(reference, changed)


def test_added_or_removed_cases_require_explicit_update(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    _save(path, _measured())
    reference = baseline.read_baseline(path)
    for names in (("a",), ("a", "b", "c")):
        with pytest.raises(ValueError, match="case set/denominator"):
            baseline.compare_to_baseline(reference, _measured(("a",), names))


def test_corpus_digest_includes_fixture_bytes_and_paths(tmp_path: Path) -> None:
    folder = tmp_path / "case_a"
    folder.mkdir()
    (folder / "manifest.json").write_text('{"class":"case_a"}')
    fixture = folder / "target.py.txt"
    fixture.write_bytes(b"print(1)\n")
    digest = baseline.corpus_bytes_digest(tmp_path)
    fixture.write_bytes(b"print(2)\n")
    assert baseline.corpus_bytes_digest(tmp_path) != digest
    fixture.rename(folder / "renamed.py.txt")
    assert baseline.corpus_bytes_digest(tmp_path) != digest
    with pytest.raises(ValueError, match="corpus directory missing"):
        baseline.corpus_bytes_digest(tmp_path / "empty")  # nonexistent corpus also fails closed


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(content_sha256="0" * 64),
        lambda value: value.update(caught_count=0),
        lambda value: value.update(total=3),
        lambda value: value.update(catch_rate=0.99),
        lambda value: value.update(suite_hash="bad"),
        lambda value: value["cases"].append(dict(value["cases"][0])),
        lambda value: value["cases"].pop(),
    ],
)
def test_corrupted_baseline_rejected(tmp_path: Path, mutation) -> None:
    path = tmp_path / "baseline.json"
    _save(path, _measured())
    bad = json.loads(path.read_text())
    mutation(bad)
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        baseline.read_baseline(path)


def test_missing_and_malformed_baseline_rejected(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    with pytest.raises(ValueError, match="missing or unreadable"):
        baseline.read_baseline(path)
    path.write_text("{broken")
    with pytest.raises(ValueError, match="missing or unreadable"):
        baseline.read_baseline(path)


def test_tampered_case_cannot_be_fixed_by_rehashing(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    broken = _measured()
    broken["cases"][0]["actual_verdict"] = "fail"
    broken["cases"][0]["verdict_basis"] = "gate_verdict"
    _save(path, broken)
    with pytest.raises(ValueError, match="positive finding"):
        baseline.read_baseline(path)


def test_no_gate_is_legitimate_miss_but_tool_errors_fail(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    case = _measured()
    case["cases"][1].update(actual_verdict="no_gate", verdict_basis="no_gate")
    _save(path, case)
    assert baseline.read_baseline(path)["caught_count"] == 1
    for verdict, basis in (
        ("command_not_found", "tool_missing"),
        ("runner_error", "runner_error"),
        ("tool_error", "no_finding_signature"),
        ("tool_error", "junit_errors"),
        ("inconclusive", "unclassified_fail"),
    ):
        broken = _measured()
        broken["cases"][1].update(actual_verdict=verdict, verdict_basis=basis)
        _save(path, broken)
        with pytest.raises(ValueError, match="incomplete gate evaluation or tool/runner error"):
            baseline.read_baseline(path)


def test_missing_tool_is_rejected_before_execution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    case = GateEvasionCase(
        class_name="dead_case",
        description="dead code",
        expected_verdict="fail",
        gate_that_must_flag="dead_code",
        taxonomy_category="evasion_dead",
        case_dir=tmp_path,
        manifest_path=tmp_path / "manifest.json",
    )
    monkeypatch.setattr(baseline.importlib.util, "find_spec", lambda name: None if name == "vulture" else object())
    with pytest.raises(ValueError, match="vulture.*missing"):
        baseline.required_tool_versions([case])


def test_measure_rejects_duplicate_and_incomplete_cases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("one", "two"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "manifest.json").write_text(
            json.dumps({"class": "duplicated", "gate_that_must_flag": "lint", "expected_verdict": "fail"})
        )
        (folder / "source.py").write_text("pass\n")
    with pytest.raises(ValueError, match="duplicate"):
        baseline.measure_gate_evasion(tmp_path)
    (tmp_path / "two" / "manifest.json").write_text(
        json.dumps({"class": "two", "gate_that_must_flag": "lint", "expected_verdict": "fail"})
    )
    (tmp_path / "one" / "manifest.json").write_text(
        json.dumps({"class": "one", "gate_that_must_flag": "lint", "expected_verdict": "fail"})
    )
    monkeypatch.setattr(
        baseline,
        "run_gate_evasion_suite",
        lambda corpus_dir: (
            score_gate_evasion(
                [
                    GateEvasionResult(
                        case_class="one",
                        gate_that_must_flag="lint",
                        expected_verdict="fail",
                        caught=False,
                        actual_verdict="pass",
                        verdict_basis="gate_verdict",
                    )
                ]
            ),
            None,
        ),
    )
    with pytest.raises(ValueError, match="every case exactly once"):
        baseline.measure_gate_evasion(tmp_path)


def test_runner_error_never_becomes_neutral(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = tmp_path / "one"
    folder.mkdir()
    (folder / "manifest.json").write_text(
        json.dumps({"class": "one", "gate_that_must_flag": "lint", "expected_verdict": "fail"})
    )
    (folder / "source.py").write_text("pass\n")
    monkeypatch.setattr(
        baseline,
        "run_gate_evasion_suite",
        lambda corpus_dir: (
            score_gate_evasion(
                [
                    GateEvasionResult(
                        case_class="one",
                        gate_that_must_flag="lint",
                        expected_verdict="fail",
                        caught=False,
                        actual_verdict="runner_error",
                        verdict_basis="runner_error",
                    )
                ]
            ),
            None,
        ),
    )
    with pytest.raises(ValueError, match="runner error"):
        baseline.measure_gate_evasion(tmp_path)


def test_cli_requires_explicit_reason_and_never_updates_during_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(baseline, "measure_gate_evasion", lambda: _measured())
    path = tmp_path / "baseline.json"
    runner = CliRunner()
    options = ["gate-evasion-baseline", "--baseline", str(path)]
    assert runner.invoke(bench_group, [*options, "--update"]).exit_code != 0
    result = runner.invoke(bench_group, [*options, "--update", "--reason", "Approved new corpus"])
    assert result.exit_code == 0, result.output
    original_bytes = path.read_bytes()
    assert baseline.read_baseline(path)["update_reason"] == "Approved new corpus"
    assert runner.invoke(bench_group, options).exit_code == 0
    assert path.read_bytes() == original_bytes
    monkeypatch.setenv("CI", "true")
    assert runner.invoke(bench_group, [*options, "--update", "--reason", "CI is forbidden"]).exit_code != 0
    assert path.read_bytes() == original_bytes


def test_cli_invalid_baseline_or_runner_error_exits_nonzero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    options = ["gate-evasion-baseline", "--baseline", str(path)]
    runner = CliRunner()
    assert runner.invoke(bench_group, options).exit_code != 0
    _save(path, _measured())
    monkeypatch.setattr(baseline, "measure_gate_evasion", lambda: (_ for _ in ()).throw(RuntimeError("gate broken")))
    result = runner.invoke(bench_group, options)
    assert result.exit_code != 0
    assert "gate broken" in result.output
