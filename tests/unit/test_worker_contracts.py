"""Unit tests for the worker terminal-payload contract schema (#2244).

Covers:
    * ``WorkerCompletion`` parsing - required fields, length caps, typed
      schema-error paths.
    * ``WorkerRefusal`` parsing - closed kind enum, per-kind payload
      requirements, unknown kinds rejected as contract violations.
    * Payload dispatch (completion vs refusal) and malformed-JSON handling.
    * Deterministic follow-up derivation for ``scope_exceeded`` refusals:
      the same payload always yields the same follow-up task set.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bernstein.core.tasks.contracts import (
    SUMMARY_MAX_CHARS,
    WORKER_CONTRACT_VERSION,
    ContractViolation,
    RefusalKind,
    WorkerCompletion,
    WorkerRefusal,
    derive_follow_up_specs,
    looks_like_contract_payload,
    parse_terminal_payload,
    parse_terminal_payload_text,
)

# ---------------------------------------------------------------------------
# Contract version constant
# ---------------------------------------------------------------------------


def test_contract_version_is_stable_string() -> None:
    assert WORKER_CONTRACT_VERSION == "worker-completion/v1"


# ---------------------------------------------------------------------------
# WorkerCompletion parsing
# ---------------------------------------------------------------------------


def _completion_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "contract": WORKER_CONTRACT_VERSION,
        "summary": "Implemented the feature and ran the tests.",
        "files_changed": ["src/foo.py", "tests/test_foo.py"],
        "verification": {"command": "pytest tests/test_foo.py", "exit_code": 0},
        "receipt_ref": "sha256:abc123",
    }
    base.update(overrides)
    return base


class TestParseCompletion:
    def test_full_payload_round_trips(self) -> None:
        result = parse_terminal_payload(_completion_payload())
        assert isinstance(result, WorkerCompletion)
        assert result.summary.startswith("Implemented")
        assert result.files_changed == ("src/foo.py", "tests/test_foo.py")
        assert result.verification is not None
        assert result.verification.command == "pytest tests/test_foo.py"
        assert result.verification.exit_code == 0
        assert result.receipt_ref == "sha256:abc123"

    def test_minimal_payload_only_summary(self) -> None:
        result = parse_terminal_payload({"summary": "Done."})
        assert isinstance(result, WorkerCompletion)
        assert result.files_changed == ()
        assert result.verification is None
        assert result.receipt_ref is None

    def test_non_object_payload_rejected(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(["not", "an", "object"])
        assert exc_info.value.path == "$"

    def test_missing_summary_rejected_with_path(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload({"files_changed": []})
        assert exc_info.value.path == "$.summary"

    def test_empty_summary_rejected(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_completion_payload(summary="   "))
        assert exc_info.value.path == "$.summary"

    def test_summary_length_capped(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_completion_payload(summary="x" * (SUMMARY_MAX_CHARS + 1)))
        assert exc_info.value.path == "$.summary"

    def test_summary_at_cap_accepted(self) -> None:
        result = parse_terminal_payload(_completion_payload(summary="x" * SUMMARY_MAX_CHARS))
        assert isinstance(result, WorkerCompletion)

    def test_files_changed_must_be_list(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_completion_payload(files_changed="src/foo.py"))
        assert exc_info.value.path == "$.files_changed"

    def test_files_changed_element_must_be_non_empty_string(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_completion_payload(files_changed=["src/foo.py", ""]))
        assert exc_info.value.path == "$.files_changed[1]"

    def test_verification_requires_command(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_completion_payload(verification={"exit_code": 0}))
        assert exc_info.value.path == "$.verification.command"

    def test_verification_exit_code_must_be_int(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_completion_payload(verification={"command": "pytest", "exit_code": "0"}))
        assert exc_info.value.path == "$.verification.exit_code"

    def test_verification_exit_code_bool_rejected(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_completion_payload(verification={"command": "pytest", "exit_code": True}))
        assert exc_info.value.path == "$.verification.exit_code"

    def test_verification_none_allowed(self) -> None:
        result = parse_terminal_payload(_completion_payload(verification=None))
        assert isinstance(result, WorkerCompletion)
        assert result.verification is None

    def test_unknown_top_level_field_rejected(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_completion_payload(extra_field="nope"))
        assert exc_info.value.path == "$.extra_field"

    def test_wrong_contract_version_rejected(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_completion_payload(contract="worker-completion/v999"))
        assert exc_info.value.path == "$.contract"


# ---------------------------------------------------------------------------
# WorkerRefusal parsing
# ---------------------------------------------------------------------------


def _refusal_payload(kind: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "contract": WORKER_CONTRACT_VERSION,
        "kind": kind,
        "detail": "Cannot proceed as specified.",
    }
    per_kind: dict[str, dict[str, Any]] = {
        "scope_exceeded": {"proposed_split": ["part one", "part two"]},
        "underspecified": {"question": "Which auth backend should this target?"},
        "awaiting_operator": {"question": "May I delete the legacy config?"},
        "blocked_on_dependency": {"blocking_dep": "T-42"},
    }
    base.update(per_kind.get(kind, {}))
    base.update(overrides)
    return base


class TestParseRefusal:
    @pytest.mark.parametrize(
        "kind",
        ["awaiting_operator", "scope_exceeded", "underspecified", "blocked_on_dependency"],
    )
    def test_all_kinds_parse(self, kind: str) -> None:
        result = parse_terminal_payload(_refusal_payload(kind))
        assert isinstance(result, WorkerRefusal)
        assert result.kind is RefusalKind(kind)
        assert result.detail

    def test_unknown_kind_is_contract_violation(self) -> None:
        payload = _refusal_payload("scope_exceeded")
        payload["kind"] = "gave_up"
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(payload)
        assert exc_info.value.path == "$.kind"

    def test_kind_must_be_string(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload({"kind": 3, "detail": "x"})
        assert exc_info.value.path == "$.kind"

    def test_missing_detail_rejected(self) -> None:
        payload = _refusal_payload("blocked_on_dependency")
        del payload["detail"]
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(payload)
        assert exc_info.value.path == "$.detail"

    def test_scope_exceeded_requires_proposed_split(self) -> None:
        payload = _refusal_payload("scope_exceeded")
        del payload["proposed_split"]
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(payload)
        assert exc_info.value.path == "$.proposed_split"

    def test_scope_exceeded_rejects_empty_split(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_refusal_payload("scope_exceeded", proposed_split=[]))
        assert exc_info.value.path == "$.proposed_split"

    def test_scope_exceeded_rejects_blank_split_item(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_refusal_payload("scope_exceeded", proposed_split=["ok", " "]))
        assert exc_info.value.path == "$.proposed_split[1]"

    @pytest.mark.parametrize("kind", ["underspecified", "awaiting_operator"])
    def test_question_required(self, kind: str) -> None:
        payload = _refusal_payload(kind)
        del payload["question"]
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(payload)
        assert exc_info.value.path == "$.question"

    def test_blocked_on_dependency_requires_blocking_dep(self) -> None:
        payload = _refusal_payload("blocked_on_dependency")
        del payload["blocking_dep"]
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(payload)
        assert exc_info.value.path == "$.blocking_dep"

    def test_cross_kind_field_rejected(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload(_refusal_payload("underspecified", proposed_split=["a"]))
        assert exc_info.value.path == "$.proposed_split"

    def test_to_dict_round_trip(self) -> None:
        refusal = parse_terminal_payload(_refusal_payload("scope_exceeded"))
        assert isinstance(refusal, WorkerRefusal)
        again = parse_terminal_payload(refusal.to_dict())
        assert again == refusal


# ---------------------------------------------------------------------------
# Text-form parsing (result_summary carrying inline JSON)
# ---------------------------------------------------------------------------


class TestTextPayload:
    def test_malformed_json_is_contract_violation(self) -> None:
        with pytest.raises(ContractViolation) as exc_info:
            parse_terminal_payload_text('{"summary": "unterminated')
        assert exc_info.value.path == "$"

    def test_valid_json_completion_parses(self) -> None:
        raw = json.dumps(_completion_payload())
        result = parse_terminal_payload_text(raw)
        assert isinstance(result, WorkerCompletion)

    def test_looks_like_contract_payload(self) -> None:
        assert looks_like_contract_payload('{"summary": "done"}')
        assert looks_like_contract_payload('  {"kind": "underspecified"}  ')
        # Truncated JSON is still an attempted payload - it must surface
        # as a contract violation, not sneak through as prose.
        assert looks_like_contract_payload('{"summary": "unterminated')
        assert not looks_like_contract_payload("Implemented the parser.")
        assert not looks_like_contract_payload("")


# ---------------------------------------------------------------------------
# Deterministic follow-up derivation (AC3)
# ---------------------------------------------------------------------------


class TestFollowUpDerivation:
    def _refusal(self) -> WorkerRefusal:
        parsed = parse_terminal_payload(
            _refusal_payload("scope_exceeded", proposed_split=["extract the parser", "wire the endpoint"])
        )
        assert isinstance(parsed, WorkerRefusal)
        return parsed

    def test_same_payload_yields_identical_specs(self) -> None:
        first = derive_follow_up_specs("T-1", self._refusal())
        second = derive_follow_up_specs("T-1", self._refusal())
        assert first == second
        assert len(first) == 2

    def test_ids_are_deterministic_and_distinct(self) -> None:
        specs = derive_follow_up_specs("T-1", self._refusal())
        ids = [s.task_id for s in specs]
        assert len(set(ids)) == 2
        # Re-derivation from an equal payload must not mint new ids.
        assert ids == [s.task_id for s in derive_follow_up_specs("T-1", self._refusal())]

    def test_parent_id_participates_in_derivation(self) -> None:
        a = derive_follow_up_specs("T-1", self._refusal())
        b = derive_follow_up_specs("T-2", self._refusal())
        assert {s.task_id for s in a}.isdisjoint({s.task_id for s in b})

    def test_split_text_carried_into_spec(self) -> None:
        specs = derive_follow_up_specs("T-1", self._refusal())
        assert specs[0].title == "extract the parser"
        assert "extract the parser" in specs[0].description

    def test_non_scope_exceeded_yields_no_specs(self) -> None:
        parsed = parse_terminal_payload(_refusal_payload("underspecified"))
        assert isinstance(parsed, WorkerRefusal)
        assert derive_follow_up_specs("T-1", parsed) == []
