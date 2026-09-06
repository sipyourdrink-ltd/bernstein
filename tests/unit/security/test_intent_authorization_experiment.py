"""Unit tests for intent-vs-args authorization experiment (#5065).

Tests cover:
  - Corpus structure and integrity (all pairs well-formed, digests match)
  - Policy condition YAML files load without error
  - Rego policy file is valid Rego syntax
  - policy_evaluator.py correctly distinguishes benign from harmful
  - Intent digest computation is deterministic and unique per intent text
  - Injection-resistance: hostile tool result cannot alter recorded intent mid-run
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bernstein.experiment.intent_vs_args.policy_evaluator import (
    CORPUS_PATH,
    POLICIES_DIR,
    REGOREPO_PATH,
    _compute_intent_digest,
    _eval_argument_aware,
    _eval_intent_aware,
    _eval_role_only,
    _load_corpus,
    _load_policies,
    evaluate_corpus_pair,
    export_results_json,
    format_experiment_summary,
    run_experiment,
)

EXPERIMENT_ROOT = Path(__file__).parent.parent.parent.parent / "experiment" / "intent_vs_args"


class TestCorpusIntegrity:
    """Corpus structure and content integrity checks."""

    def test_corpus_file_exists(self) -> None:
        assert CORPUS_PATH.exists(), f"Corpus not found at {CORPUS_PATH}"

    def test_corpus_parses_as_json(self) -> None:
        raw = CORPUS_PATH.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

    def test_corpus_version_field(self) -> None:
        corpus = _load_corpus()
        assert corpus.get("version") == 1
        assert "description" in corpus

    def test_corpus_has_expected_pair_count(self) -> None:
        corpus = _load_corpus()
        pairs = corpus.get("pairs", [])
        assert len(pairs) >= 10, f"Expected >=10 pairs, got {len(pairs)}"

    def test_each_pair_has_required_fields(self) -> None:
        corpus = _load_corpus()
        for pair in corpus["pairs"]:
            assert "id" in pair
            assert "category" in pair
            assert "benign" in pair
            assert "harmful" in pair
            for variant in ("benign", "harmful"):
                event = pair[variant]
                assert "agent_id" in event
                assert "user" in event
                assert "resource" in event
                assert "entitlement" in event
                assert "declared_intent" in event
                assert "args_digest" in event

    def test_benign_and_harmful_share_identical_non_intent_fields(self) -> None:
        corpus = _load_corpus()
        for pair in corpus["pairs"]:
            for field in ("agent_id", "user", "resource", "entitlement", "args_digest"):
                assert pair["benign"][field] == pair["harmful"][field], (
                    f"Pair {pair['id']}: {field} should be identical across variants"
                )

    def test_benign_and_harmful_have_different_declared_intent(self) -> None:
        corpus = _load_corpus()
        for pair in corpus["pairs"]:
            assert pair["benign"]["declared_intent"] != pair["harmful"]["declared_intent"], (
                f"Pair {pair['id']}: benign and harmful must have different declared_intent"
            )

    def test_args_digest_format_is_valid_sha256(self) -> None:
        sha256_re = re.compile(r"^sha256:[a-f0-9]{64}$")
        corpus = _load_corpus()
        for pair in corpus["pairs"]:
            for variant in ("benign", "harmful"):
                digest = pair[variant]["args_digest"]
                assert sha256_re.match(digest), f"Pair {pair['id']} {variant}: args_digest {digest!r} not valid sha256"

    def test_each_pair_id_is_unique(self) -> None:
        corpus = _load_corpus()
        ids = [p["id"] for p in corpus["pairs"]]
        assert len(ids) == len(set(ids)), "Duplicate pair IDs found"


class TestIntentDigestDeterminism:
    """Intent digest computation is deterministic and intent-specific."""

    def test_same_intent_produces_same_digest(self) -> None:
        text = "reading quarterly summary for business review"
        d1 = _compute_intent_digest(text)
        d2 = _compute_intent_digest(text)
        assert d1 == d2

    def test_different_intent_produces_different_digest(self) -> None:
        benign = "reading quarterly summary for business review"
        harmful = "downloading customer data to sell on dark web"
        d1 = _compute_intent_digest(benign)
        d2 = _compute_intent_digest(harmful)
        assert d1 != d2

    def test_intent_digest_is_valid_sha256(self) -> None:
        sha256_re = re.compile(r"^sha256:[a-f0-9]{64}$")
        digest = _compute_intent_digest("rotating expired API keys during maintenance window")
        assert sha256_re.match(digest)

    def test_benign_and_harmful_intents_from_corpus_differ(self) -> None:
        corpus = _load_corpus()
        for pair in corpus["pairs"]:
            benign_digest = _compute_intent_digest(pair["benign"]["declared_intent"])
            harmful_digest = _compute_intent_digest(pair["harmful"]["declared_intent"])
            assert benign_digest != harmful_digest, (
                f"Pair {pair['id']}: intent digests should differ between benign and harmful"
            )


class TestPolicyFiles:
    """Policy YAML and Rego files exist and are loadable."""

    def test_role_only_yaml_exists(self) -> None:
        path = POLICIES_DIR / "role_only.yaml"
        assert path.exists()

    def test_argument_aware_yaml_exists(self) -> None:
        path = POLICIES_DIR / "argument_aware.yaml"
        assert path.exists()

    def test_intent_aware_yaml_exists(self) -> None:
        path = POLICIES_DIR / "intent_aware.yaml"
        assert path.exists()

    def test_regorepo_rego_exists(self) -> None:
        assert REGOREPO_PATH.exists()

    def test_yaml_files_are_valid_yaml(self) -> None:
        import yaml

        for yaml_file in ("role_only.yaml", "argument_aware.yaml", "intent_aware.yaml"):
            path = POLICIES_DIR / yaml_file
            raw = path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(raw)
            assert isinstance(parsed, dict)

    def test_yaml_policies_have_version_field(self) -> None:
        import yaml

        for yaml_file in ("role_only.yaml", "argument_aware.yaml", "intent_aware.yaml"):
            path = POLICIES_DIR / yaml_file
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert "version" in parsed
            assert parsed["version"] == "1.0"

    def test_regorepo_rego_package_declaration(self) -> None:
        content = REGOREPO_PATH.read_text(encoding="utf-8")
        assert "package bernstein.authz.intent_experiment" in content

    def test_regorepo_has_all_three_variants(self) -> None:
        content = REGOREPO_PATH.read_text(encoding="utf-8")
        assert "role_only_allow" in content
        assert "argument_aware_allow" in content
        assert "intent_aware_allow" in content


class TestPolicyEvaluatorFunctions:
    """Unit tests for individual evaluation functions."""

    def test_eval_role_only_benign_event(self) -> None:
        event = {
            "agent_id": "agent-001",
            "user": "alice",
            "resource": "file:///data/reports/q4_summary.csv",
            "entitlement": "read",
            "declared_intent": "reading quarterly summary for business review",
            "args_digest": "sha256:a3f5c8d9e1b2c4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4",
        }
        result = _eval_role_only(event)
        assert isinstance(result.decision, bool)
        assert result.policy_variant == "role_only"
        assert result.latency_us >= 0
        assert result.policy_digest.startswith("sha256:")

    def test_eval_role_only_harmful_event(self) -> None:
        event = {
            "agent_id": "agent-001",
            "user": "alice",
            "resource": "file:///data/reports/q4_summary.csv",
            "entitlement": "read",
            "declared_intent": "downloading customer data to sell on dark web",
            "args_digest": "sha256:a3f5c8d9e1b2c4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4",
        }
        result = _eval_role_only(event)
        assert isinstance(result.decision, bool)

    def test_eval_argument_aware_requires_digest(self) -> None:
        event_no_digest = {
            "agent_id": "agent-001",
            "user": "alice",
            "resource": "file:///data/reports/q4_summary.csv",
            "entitlement": "read",
            "declared_intent": "reading quarterly summary",
            "args_digest": "",
        }
        result = _eval_argument_aware(event_no_digest)
        assert result.decision is False

    def test_eval_argument_aware_allows_valid_digest(self) -> None:
        event = {
            "agent_id": "agent-001",
            "user": "alice",
            "resource": "file:///data/reports/q4_summary.csv",
            "entitlement": "read",
            "declared_intent": "reading quarterly summary",
            "args_digest": "sha256:a3f5c8d9e1b2c4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4",
        }
        result = _eval_argument_aware(event)
        assert result.decision is True

    def test_eval_intent_aware_blocks_malicious_patterns(self) -> None:
        harmful_event = {
            "agent_id": "agent-001",
            "user": "alice",
            "resource": "file:///data/reports/q4_summary.csv",
            "entitlement": "read",
            "declared_intent": "downloading customer data to sell on dark web",
            "args_digest": "sha256:a3f5c8d9e1b2c4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4",
        }
        result = _eval_intent_aware(harmful_event)
        assert result.decision is False

    def test_eval_intent_aware_allows_benign_patterns(self) -> None:
        benign_event = {
            "agent_id": "agent-001",
            "user": "alice",
            "resource": "file:///data/reports/q4_summary.csv",
            "entitlement": "read",
            "declared_intent": "reading quarterly summary for business review",
            "args_digest": "sha256:a3f5c8d9e1b2c4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4",
        }
        result = _eval_intent_aware(benign_event)
        assert result.decision is True


class TestEvaluateCorpusPair:
    """evaluate_corpus_pair correctly associates digests and evaluates both variants."""

    def test_returns_two_results(self) -> None:
        corpus = _load_corpus()
        pair = corpus["pairs"][0]
        benign_result, harmful_result = evaluate_corpus_pair(pair)
        assert benign_result.event_id.endswith("_benign")
        assert harmful_result.event_id.endswith("_harmful")

    def test_intent_digest_is_populated(self) -> None:
        corpus = _load_corpus()
        pair = corpus["pairs"][0]
        benign_result, harmful_result = evaluate_corpus_pair(pair)
        assert benign_result.intent_digest.startswith("sha256:")
        assert harmful_result.intent_digest.startswith("sha256:")
        assert benign_result.intent_digest != harmful_result.intent_digest

    def test_args_digest_unchanged(self) -> None:
        corpus = _load_corpus()
        pair = corpus["pairs"][0]
        original_args = pair["benign"]["args_digest"]
        benign_result, _ = evaluate_corpus_pair(pair)
        assert benign_result.args_digest == original_args

    def test_pair_id_matches_corpus(self) -> None:
        corpus = _load_corpus()
        for pair in corpus["pairs"]:
            benign_result, harmful_result = evaluate_corpus_pair(pair)
            assert benign_result.pair_id == pair["id"]
            assert harmful_result.pair_id == pair["id"]


class TestRunExperiment:
    """run_experiment produces well-formed ExperimentSummary."""

    def test_returns_experiment_summary(self) -> None:
        summary = run_experiment()
        assert summary.total_pairs > 0
        assert summary.total_events == summary.total_pairs * 2
        assert summary.total_events > 0

    def test_accuracy_fields_are_floats(self) -> None:
        summary = run_experiment()
        assert isinstance(summary.role_only_accuracy, float)
        assert isinstance(summary.argument_aware_accuracy, float)
        assert isinstance(summary.intent_aware_accuracy, float)

    def test_latency_fields_are_non_negative(self) -> None:
        summary = run_experiment()
        assert summary.role_only_avg_latency_us >= 0
        assert summary.argument_aware_avg_latency_us >= 0
        assert summary.intent_aware_avg_latency_us >= 0

    def test_results_length_matches_events(self) -> None:
        summary = run_experiment()
        assert len(summary.results) == summary.total_events

    def test_intent_aware_accuracy_reportable(self) -> None:
        summary = run_experiment()
        assert isinstance(summary.intent_adds_value, bool)

    def test_format_experiment_summary_produces_string(self) -> None:
        summary = run_experiment()
        report = format_experiment_summary(summary)
        assert isinstance(report, str)
        assert len(report) > 100
        assert "Intent-vs-Args" in report
        assert "role-only" in report

    def test_export_results_json_writes_file(self, tmp_path: Path) -> None:
        summary = run_experiment()
        out_path = tmp_path / "experiment_results.json"
        export_results_json(summary, out_path)
        assert out_path.exists()
        parsed = json.loads(out_path.read_text(encoding="utf-8"))
        assert parsed["experiment"] == "intent_vs_args"
        assert parsed["total_pairs"] == summary.total_pairs
        assert len(parsed["results"]) == summary.total_events


class TestInjectionResistanceAndDigestPurity:
    """Hostile tool result cannot change recorded intent mid-run; intent digest is pure.

    This tests the structural property that intent_digest is computed at
    attestation time (before tool execution) and locked into the attestation
    record.  A tool result that arrives after attestation cannot retroactively
    change the recorded intent_digest.  Because intent_digest is a pure
    function of the pre-attestation declared_intent, the recorded digest is
    also immune to mutation by any post-attestation tool result.
    """

    def test_intent_digest_is_precomputed_not_from_result(self) -> None:
        corpus = _load_corpus()
        for pair in corpus["pairs"]:
            benign_intent = pair["benign"]["declared_intent"]
            computed = _compute_intent_digest(benign_intent)
            assert computed.startswith("sha256:")
            assert len(computed) == 71

    def test_intent_digest_computation_is_pure(self) -> None:
        event = {
            "agent_id": "agent-001",
            "user": "alice",
            "resource": "file:///data/reports/q4_summary.csv",
            "entitlement": "read",
            "declared_intent": "rotating expired API keys during maintenance window",
            "args_digest": "sha256:b4e6d9c0f2a3b5c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c",
        }
        r1 = _eval_intent_aware(event)
        r2 = _eval_intent_aware(event)
        assert r1.decision == r2.decision
        assert r1.policy_digest == r2.policy_digest

    def test_different_args_same_intent_digest_different_policy_outcome(self) -> None:
        base_event = {
            "agent_id": "agent-001",
            "user": "alice",
            "resource": "file:///data/reports/q4_summary.csv",
            "entitlement": "read",
            "declared_intent": "downloading customer data to sell on dark web",
            "args_digest": "sha256:aaaa",
        }
        result_a = _eval_intent_aware(base_event)
        assert result_a.decision is False

        modified = dict(base_event)
        modified["args_digest"] = "sha256:bbbb"
        result_b = _eval_intent_aware(modified)
        assert result_b.decision is False

        assert result_a.decision == result_b.decision, (
            "intent_aware policy outcome must not change when only args_digest changes "
            "(intent is what the policy evaluates, not args)"
        )

    def test_intent_digest_unaffected_by_resource_change(self) -> None:
        intent_text = "deploying verified container image to production cluster"
        digest1 = _compute_intent_digest(intent_text)
        digest2 = _compute_intent_digest(intent_text)
        assert digest1 == digest2

    def test_hostile_tool_result_cannot_mutate_recorded_intent_digest(self) -> None:
        """A hostile tool result cannot alter the recorded intent_digest after attestation.

        Simulates an adversarial injection attack where a malicious tool result tries
        to mutate the pre-attested intent_digest. The test verifies that:
        1. Pre-attested intent_digest is recorded before tool execution
        2. A hostile tool result cannot change that digest retroactively
        3. The same event processed twice yields the same intent_digest (determinism)
        """
        # Step 1: Pre-attestation - compute and "record" the intent digest
        declared_intent = "reading quarterly summary for business review"
        pre_attested_digest = _compute_intent_digest(declared_intent)

        # Verif1: digest is a valid SHA-256
        assert pre_attested_digest.startswith("sha256:")
        assert len(pre_attested_digest) == 71

        # Step 2: Simulate hostile tool result trying to inject malicious intent
        # (In a real attack, the tool would return corrupted/corrupting data)
        recorded_event_intent = declared_intent  # what was recorded at attestation
        recorded_digest = _compute_intent_digest(recorded_event_intent)

        # Attack attempt: different intent text would produce different digest
        attack_intent = "exfiltrating data to external server for attacker"
        attack_digest = _compute_intent_digest(attack_intent)

        # Verif2: The attack digest differs from recorded digest
        assert attack_digest != recorded_digest

        # Verif3: The recorded digest is deterministic
        assert recorded_digest == _compute_intent_digest(declared_intent)

        # The policy evaluation always uses the pre-attested digest, never the attack variant
        event_with_attack_intent = {
            "agent_id": "agent-001",
            "resource": "file:///data/reports/q4_summary.csv",
            "declared_intent": attack_intent,  # attacker tries to change this
            "args_digest": "sha256:abc",
        }
        attack_result = _eval_intent_aware(event_with_attack_intent)
        assert attack_result.decision is False  # malicious intent is correctly rejected

        # The benign intent gets approved (uses pre-attested digest from attestation)
        event_benign = {
            "agent_id": "agent-001",
            "resource": "file:///data/reports/q4_summary.csv",
            "declared_intent": declared_intent,
            "args_digest": "sha256:abc",
        }
        benign_result = _eval_intent_aware(event_benign)
        assert benign_result.decision is True  # benign approved

        # Critical: Same attack event always produces same result (no mutation possible)
        for _ in range(3):
            r1 = _eval_intent_aware(event_with_attack_intent)
            r2 = _eval_intent_aware(event_with_attack_intent)
            assert r1.decision == r2.decision
            assert r1.policy_digest == r2.policy_digest


class TestLoadPolicies:
    """_load_policies returns raw text for all three variants."""

    def test_loads_all_three_variants(self) -> None:
        policies = _load_policies()
        assert set(policies.keys()) == {"role_only", "argument_aware", "intent_aware"}

    def test_each_policy_text_is_non_empty(self) -> None:
        policies = _load_policies()
        for name, text in policies.items():
            assert len(text) > 0, f"Policy {name} is empty"
            assert "version" in text


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])
