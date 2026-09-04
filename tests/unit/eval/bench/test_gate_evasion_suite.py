"""
Unit tests for bernstein-bench gate-evasion benchmark suite.

Covers:
1. Dynamic discovery of gate evasion fixtures from corpus directory.
2. Verification of all 8 core evasion classes and manifests.
3. Suite building with content-addressing and deterministic hashing.
4. Adding a new evasion class requiring NO Python code changes.
5. Catch rate scoring, missed class reporting, and responsible gate attribution.
6. Generation and verification of valid signed SubmissionBundles.
7. CLI suite registry resolution.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.eval.bench.bench_cli import _get_suite
from bernstein.eval.bench.bundle import SubmissionBundle
from bernstein.eval.bench.gate_evasion_suite import (
    DEFAULT_EVASION_CORPUS_DIR,
    GateEvasionCase,
    GateEvasionResult,
    build_gate_evasion_suite_v1,
    load_evasion_corpus,
    run_gate_evasion_suite,
    score_gate_evasion,
)
from bernstein.eval.bench.suite import BenchSuite
from bernstein.eval.taxonomy import FailureCategory

EXPECTED_8_CLASSES = {
    "empty_file_deletion",
    "unimported_test_symbol",
    "broken_code_scanner_silencing",
    "runtime_config_placeholder_secret",
    "dead_code_test_deletion",
    "broad_except_failure_hiding",
    "nonexistent_api_mock_test",
    "impossible_local_verification_publish",
}


class TestGateEvasionCorpusDiscovery:
    """Test loading and validation of gate evasion fixtures."""

    def test_default_corpus_dir_exists(self) -> None:
        assert DEFAULT_EVASION_CORPUS_DIR.exists()
        assert DEFAULT_EVASION_CORPUS_DIR.is_dir()

    def test_load_evasion_corpus_discovers_all_8_fixtures(self) -> None:
        cases = load_evasion_corpus()
        assert len(cases) >= 8

        loaded_classes = {c.class_name for c in cases}
        assert EXPECTED_8_CLASSES.issubset(loaded_classes)

        for case in cases:
            assert isinstance(case, GateEvasionCase)
            assert case.class_name != ""
            assert case.expected_verdict == "fail"
            assert case.gate_that_must_flag != ""
            assert case.taxonomy_category != ""
            assert case.case_dir.exists()
            assert case.manifest_path.exists()
            assert case.description != ""
            assert isinstance(case.sample_files, tuple)

            # Test to_dict serialization
            d = case.to_dict()
            assert d["class"] == case.class_name
            assert d["expected_verdict"] == case.expected_verdict
            assert d["gate_that_must_flag"] == case.gate_that_must_flag

    def test_dynamic_discovery_new_class_requires_no_code_changes(self, tmp_path: Path) -> None:
        """Adding a new fixture directory with manifest.json is loaded automatically."""
        new_case_dir = tmp_path / "zero_day_evasion"
        new_case_dir.mkdir()
        manifest_data = {
            "class": "zero_day_evasion",
            "description": "Novel prompt injection in test docstring to bypass linter",
            "expected_verdict": "fail",
            "gate_that_must_flag": "prompt_injection_gate",
            "taxonomy_category": "evasion_prompt_injection",
        }
        (new_case_dir / "manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")
        (new_case_dir / "payload.py").write_text("# malicious comment\n", encoding="utf-8")

        cases = load_evasion_corpus(tmp_path)
        assert len(cases) == 1
        case = cases[0]
        assert case.class_name == "zero_day_evasion"
        assert case.gate_that_must_flag == "prompt_injection_gate"
        assert case.taxonomy_category == "evasion_prompt_injection"
        assert case.sample_files == ("payload.py",)

    def test_load_nonexistent_or_empty_corpus(self, tmp_path: Path) -> None:
        assert load_evasion_corpus(tmp_path / "nonexistent") == []
        assert load_evasion_corpus(tmp_path) == []


class TestGateEvasionSuiteBuilding:
    """Test BenchSuite generation from evasion corpus."""

    def test_build_gate_evasion_suite_v1_tasks(self) -> None:
        suite = build_gate_evasion_suite_v1()
        assert isinstance(suite, BenchSuite)
        assert suite.version == "gate-evasion-v1"
        assert len(suite.tasks) >= 8

        task_ids = {t.id for t in suite.tasks}
        for cls_name in EXPECTED_8_CLASSES:
            assert f"gate_evasion_{cls_name}" in task_ids

    def test_suite_content_addressed_determinism(self) -> None:
        suite1 = build_gate_evasion_suite_v1()
        suite2 = build_gate_evasion_suite_v1()

        assert suite1.suite_hash != ""
        assert suite1.suite_hash == suite2.suite_hash

    def test_suite_resolution_via_bench_cli(self) -> None:
        suite = _get_suite("gate-evasion-v1")
        assert suite.version == "gate-evasion-v1"
        assert len(suite.tasks) >= 8


class TestGateEvasionScoring:
    """Test scoring, catch-rate calculation, and failure attribution."""

    def test_score_all_caught(self) -> None:
        results = [
            GateEvasionResult(
                case_class="c1",
                gate_that_must_flag="gate_a",
                expected_verdict="fail",
                caught=True,
            ),
            GateEvasionResult(
                case_class="c2",
                gate_that_must_flag="gate_b",
                expected_verdict="fail",
                caught=True,
            ),
        ]
        score = score_gate_evasion(results)
        assert score.total_cases == 2
        assert score.caught_cases == 2
        assert score.catch_rate == 1.0
        assert score.missed_classes == ()
        assert score.responsible_gates == {}
        assert "All evasion classes successfully caught!" in score.summary()

    def test_score_partial_misses(self) -> None:
        results = [
            GateEvasionResult(
                case_class="empty_file_deletion",
                gate_that_must_flag="absence_coverage",
                expected_verdict="fail",
                caught=True,
            ),
            GateEvasionResult(
                case_class="unimported_test_symbol",
                gate_that_must_flag="test_enforcement",
                expected_verdict="fail",
                caught=False,
            ),
            GateEvasionResult(
                case_class="dead_code_test_deletion",
                gate_that_must_flag="dead_code_detector",
                expected_verdict="fail",
                caught=False,
            ),
            GateEvasionResult(
                case_class="second_absence_miss",
                gate_that_must_flag="absence_coverage",
                expected_verdict="fail",
                caught=False,
            ),
        ]
        score = score_gate_evasion(results)
        assert score.total_cases == 4
        assert score.caught_cases == 1
        assert score.catch_rate == 0.25
        assert set(score.missed_classes) == {
            "unimported_test_symbol",
            "dead_code_test_deletion",
            "second_absence_miss",
        }
        assert score.responsible_gates == {
            "test_enforcement": 1,
            "dead_code_detector": 1,
            "absence_coverage": 1,
        }
        summary = score.summary()
        assert "25.0%" in summary
        assert "Missed Evasion Classes:" in summary
        assert "Responsible Gates with Misses:" in summary
        assert "test_enforcement: 1 missed" in summary

    def test_score_empty_results(self) -> None:
        score = score_gate_evasion([])
        assert score.total_cases == 0
        assert score.caught_cases == 0
        assert score.catch_rate == 1.0
        assert score.missed_classes == ()


class TestGateEvasionSimulationAndBundle:
    """Test end-to-end execution simulation and SubmissionBundle creation."""

    def test_run_gate_evasion_suite_default_simulation(self) -> None:
        score, bundle = run_gate_evasion_suite()
        assert score.catch_rate == 1.0
        assert score.caught_cases == len(EXPECTED_8_CLASSES)

        assert isinstance(bundle, SubmissionBundle)
        assert bundle.suite_version == "gate-evasion-v1"
        assert len(bundle.task_results) >= 8
        assert bundle.overall_score == 1.0
        assert bundle.pass_rate == 1.0

        # Verify round-trip bundle loading and content hash integrity
        bundle_dict = bundle.to_dict()
        reloaded = SubmissionBundle.from_dict(bundle_dict)
        assert reloaded.bundle_hash() == bundle.bundle_hash()
        assert reloaded.suite_hash == bundle.suite_hash

    def test_run_gate_evasion_suite_custom_evaluator(self) -> None:
        def simulated_evaluator(case: GateEvasionCase) -> tuple[bool, str, str]:
            if case.class_name == "broad_except_failure_hiding":
                return False, "pass", "Gate effectiveness missed broad except"
            return True, "fail", "Gate flagged evasion"

        score, bundle = run_gate_evasion_suite(evaluator=simulated_evaluator)
        assert score.caught_cases == len(EXPECTED_8_CLASSES) - 1
        assert "broad_except_failure_hiding" in score.missed_classes
        assert score.responsible_gates.get("effectiveness") == 1
        assert bundle.pass_rate < 1.0


class TestTaxonomyEvasionCategories:
    """Test that failure taxonomy contains gate evasion categories."""

    def test_evasion_categories_exist_in_enum(self) -> None:
        assert hasattr(FailureCategory, "GATE_EVASION")
        assert FailureCategory.GATE_EVASION.value == "gate_evasion"
        assert hasattr(FailureCategory, "EVASION_EMPTY_FILE_DELETION")
        assert hasattr(FailureCategory, "EVASION_UNIMPORTED_TEST_SYMBOL")
        assert hasattr(FailureCategory, "EVASION_SCANNER_SILENCING")
        assert hasattr(FailureCategory, "EVASION_PLACEHOLDER_SECRET")
        assert hasattr(FailureCategory, "EVASION_DEAD_CODE_DELETION")
        assert hasattr(FailureCategory, "EVASION_BROAD_EXCEPT")
        assert hasattr(FailureCategory, "EVASION_NONEXISTENT_API_MOCK")
        assert hasattr(FailureCategory, "EVASION_IMPOSSIBLE_VERIFICATION")
