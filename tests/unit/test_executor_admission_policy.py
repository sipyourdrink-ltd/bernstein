"""Unit tests for the declarative executor admission policy (#4907).

Each test is named for the property it protects: the policy is fail
closed, an explicit deny cannot be re-opened by a later allow, matching
is by glob rather than substring, and every malformed declaration is
rejected at the config boundary with the offending key named.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.security.capability_matrix import EnforcementMode
from bernstein.core.security.executor_admission import (
    REASON_NO_MATCH,
    AdmissionPolicy,
    AdmissionPolicyError,
    AdmissionSubject,
)


def _subject(**overrides: str) -> AdmissionSubject:
    """Build a baseline subject, overriding individual axes."""
    fields: dict[str, str] = {
        "role": "backend",
        "adapter": "claude",
        "model": "claude-sonnet-4",
        "endpoint": "",
        "sandbox": "worktree",
        "task_type": "standard",
    }
    fields.update(overrides)
    return AdmissionSubject(**fields)


def _policy(*rules: dict[str, object], mode: str | None = None) -> AdmissionPolicy:
    """Build a policy from raw rule mappings, as YAML would supply them."""
    raw: dict[str, object] = {"rules": list(rules)}
    if mode is not None:
        raw["mode"] = mode
    return AdmissionPolicy.from_mapping(raw)


class TestFailClosed:
    """A declared policy refuses anything it does not explicitly admit."""

    def test_subject_matching_no_allow_rule_is_refused(self) -> None:
        policy = _policy({"id": "approved-adapters", "effect": "allow", "adapters": ["codex"]})

        decision = policy.evaluate(_subject(adapter="claude"))

        assert decision.allowed is False
        assert decision.rule_id == ""
        assert decision.effect == "none"
        assert decision.reason == REASON_NO_MATCH

    def test_empty_rule_list_refuses_every_subject(self) -> None:
        policy = _policy()

        assert policy.evaluate(_subject()).allowed is False


class TestDenyPrecedence:
    """An explicit deny cannot be re-opened by a later allow rule."""

    def test_explicit_deny_overrides_a_matching_allow_rule(self) -> None:
        policy = _policy(
            {"id": "all-claude", "effect": "allow", "adapters": ["claude"]},
            {"id": "no-unsandboxed", "effect": "deny", "sandboxes": ["none"]},
        )

        decision = policy.evaluate(_subject(sandbox="none"))

        assert decision.allowed is False
        assert decision.rule_id == "no-unsandboxed"
        assert decision.effect == "deny"

    def test_first_matching_allow_rule_is_the_one_recorded(self) -> None:
        policy = _policy(
            {"id": "narrow", "effect": "allow", "models": ["claude-sonnet-4"]},
            {"id": "broad", "effect": "allow", "adapters": ["claude"]},
        )

        assert policy.evaluate(_subject()).rule_id == "narrow"


class TestAxisMatching:
    """Axes match by glob; an omitted axis does not constrain the rule."""

    def test_model_pattern_matches_by_glob_not_substring(self) -> None:
        policy = _policy({"id": "sonnet-only", "effect": "allow", "models": ["claude-sonnet-*"]})

        assert policy.evaluate(_subject(model="claude-sonnet-4")).allowed is True
        assert policy.evaluate(_subject(model="a-claude-sonnet-4-clone")).allowed is False

    def test_rule_axis_left_unset_does_not_constrain_that_axis(self) -> None:
        policy = _policy({"id": "any-model-on-claude", "effect": "allow", "adapters": ["claude"]})

        assert policy.evaluate(_subject(model="anything-at-all")).allowed is True

    def test_role_scoped_rule_does_not_admit_another_role(self) -> None:
        policy = _policy(
            {
                "id": "research-only-endpoint",
                "effect": "allow",
                "roles": ["researcher"],
                "endpoints": ["https://internal.example/*"],
            }
        )
        endpoint = "https://internal.example/v1"

        assert policy.evaluate(_subject(role="researcher", endpoint=endpoint)).allowed is True
        assert policy.evaluate(_subject(role="backend", endpoint=endpoint)).allowed is False

    def test_task_type_scoped_rule_does_not_admit_another_task_type(self) -> None:
        policy = _policy(
            {"id": "research-tasks", "effect": "allow", "task_types": ["research"], "adapters": ["claude"]}
        )

        assert policy.evaluate(_subject(task_type="research")).allowed is True
        assert policy.evaluate(_subject(task_type="standard")).allowed is False

    def test_absent_endpoint_is_not_admitted_by_a_url_pattern(self) -> None:
        policy = _policy({"id": "internal-endpoints", "effect": "allow", "endpoints": ["https://internal.example/*"]})

        assert policy.evaluate(_subject(endpoint="")).allowed is False


class TestEnforcementModes:
    """Warn and off admit the spawn but still name the refusing rule."""

    def test_warn_mode_admits_but_records_the_refusing_rule(self) -> None:
        policy = _policy(
            {"id": "no-unsandboxed", "effect": "deny", "sandboxes": ["none"]},
            mode="warn",
        )

        decision = policy.evaluate(_subject(sandbox="none"))

        assert decision.allowed is True
        assert decision.rule_id == "no-unsandboxed"
        assert decision.mode is EnforcementMode.WARN
        assert "warn-only" in decision.reason

    def test_off_mode_admits_a_subject_no_rule_matched(self) -> None:
        policy = _policy(mode="off")

        decision = policy.evaluate(_subject())

        assert decision.allowed is True
        assert decision.effect == "none"
        assert "enforcement off" in decision.reason


class TestConfigBoundary:
    """Malformed declarations are rejected with the offending key named."""

    def test_unknown_admission_key_is_rejected_with_the_key_named(self) -> None:
        with pytest.raises(AdmissionPolicyError, match="unknown keys: modes"):
            AdmissionPolicy.from_mapping({"modes": "enforce", "rules": []})

    def test_unknown_rule_key_is_rejected_with_the_key_named(self) -> None:
        with pytest.raises(AdmissionPolicyError, match=r"admission\.rules\[0\] has unknown keys: providers"):
            _policy({"id": "r", "effect": "allow", "providers": ["claude"]})

    def test_duplicate_rule_id_is_rejected(self) -> None:
        with pytest.raises(AdmissionPolicyError, match="duplicate rule id 'r'"):
            _policy(
                {"id": "r", "effect": "allow", "adapters": ["claude"]},
                {"id": "r", "effect": "deny", "adapters": ["codex"]},
            )

    def test_unconstrained_allow_rule_is_rejected(self) -> None:
        with pytest.raises(AdmissionPolicyError, match="must constrain at least one axis"):
            _policy({"id": "everything", "effect": "allow"})

    def test_unknown_effect_is_rejected(self) -> None:
        with pytest.raises(AdmissionPolicyError, match="effect must be 'allow' or 'deny'"):
            _policy({"id": "r", "effect": "permit", "adapters": ["claude"]})

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(AdmissionPolicyError, match="admission.mode must be one of"):
            _policy(mode="lenient")

    def test_non_string_pattern_is_rejected(self) -> None:
        with pytest.raises(AdmissionPolicyError, match=r"admission\.rules\[0\]\.adapters entries must be"):
            _policy({"id": "r", "effect": "allow", "adapters": [7]})


class TestLoad:
    """Loading reads the live config file and fails closed on damage."""

    def test_config_without_admission_block_yields_no_policy(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text("goal: ship it\n", encoding="utf-8")

        assert AdmissionPolicy.load(tmp_path) is None

    def test_missing_config_file_yields_no_policy(self, tmp_path: Path) -> None:
        assert AdmissionPolicy.load(tmp_path) is None

    def test_malformed_admission_block_raises_rather_than_disabling_the_gate(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text(
            "goal: ship it\nadmission:\n  rules:\n    - id: r\n      effect: nope\n",
            encoding="utf-8",
        )

        with pytest.raises(AdmissionPolicyError):
            AdmissionPolicy.load(tmp_path)

    def test_loaded_policy_evaluates_the_declared_rules(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text(
            "goal: ship it\n"
            "admission:\n"
            "  mode: enforce\n"
            "  rules:\n"
            "    - id: approved\n"
            "      effect: allow\n"
            "      adapters: [claude]\n",
            encoding="utf-8",
        )

        policy = AdmissionPolicy.load(tmp_path)

        assert policy is not None
        assert policy.evaluate(_subject()).rule_id == "approved"
        assert policy.evaluate(_subject(adapter="codex")).allowed is False
