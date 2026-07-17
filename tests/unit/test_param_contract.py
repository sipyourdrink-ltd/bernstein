"""Tests for the shared typed parameter contract (#2545, AC1 + AC5).

The param contract is the input-side vocabulary: a set of typed parameter
declarations plus a content-addressed ``params_hash`` that folds the declared
type and coerced value of every parameter into one digest. These tests pin the
determinism the fire projection depends on and the backward-compatible empty
case.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bernstein.core.tasks.param_contract import (
    PARAM_TYPE_VOCABULARY,
    ParamContract,
    ParamContractError,
    ParamContractViolation,
    ParamSpec,
    coerce_value,
)


def _full_vocab_contract() -> ParamContract:
    return ParamContract.from_schema(
        [
            {"name": "target", "type": "string", "required": True},
            {"name": "retries", "type": "int", "default": 3},
            {"name": "threshold", "type": "float", "default": 0.5},
            {"name": "dry_run", "type": "bool", "default": False},
            {"name": "mode", "type": "string", "choices": ["fast", "safe"], "default": "safe"},
        ]
    )


# ---------------------------------------------------------------------------
# Coercion across the full type vocabulary
# ---------------------------------------------------------------------------


def test_coerce_covers_full_vocabulary() -> None:
    assert coerce_value("hello", "string") == "hello"
    assert coerce_value("42", "int") == 42
    assert coerce_value("1.5", "float") == 1.5
    assert coerce_value("yes", "bool") is True
    assert coerce_value("off", "bool") is False


@pytest.mark.parametrize("bad_type", ["int", "float", "bool"])
def test_coerce_rejects_bad_values(bad_type: str) -> None:
    with pytest.raises(ParamContractError):
        coerce_value("not-a-number", bad_type)  # type: ignore[arg-type]


def test_coerce_rejects_non_finite_float() -> None:
    for token in ("nan", "inf", "-inf", "Infinity"):
        with pytest.raises(ParamContractError):
            coerce_value(token, "float")


# ---------------------------------------------------------------------------
# AC1 determinism
# ---------------------------------------------------------------------------


def test_params_hash_is_stable_across_calls() -> None:
    contract = _full_vocab_contract()
    validated = contract.validate_and_coerce({"target": "svc-a"})
    h1 = contract.params_hash(validated)
    h2 = contract.params_hash(dict(validated))
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_params_hash_independent_of_insertion_order() -> None:
    contract = _full_vocab_contract()
    a = contract.validate_and_coerce({"target": "svc", "retries": "2", "dry_run": "true"})
    b = contract.validate_and_coerce({"dry_run": "true", "retries": "2", "target": "svc"})
    assert contract.params_hash(a) == contract.params_hash(b)


@pytest.mark.parametrize(
    ("override", "changed"),
    [
        ({"target": "svc-a"}, {"target": "svc-b"}),
        ({"target": "svc", "retries": "3"}, {"target": "svc", "retries": "4"}),
        ({"target": "svc", "threshold": "0.5"}, {"target": "svc", "threshold": "0.6"}),
        ({"target": "svc", "dry_run": "false"}, {"target": "svc", "dry_run": "true"}),
        ({"target": "svc", "mode": "safe"}, {"target": "svc", "mode": "fast"}),
    ],
)
def test_changing_any_param_changes_hash(override: dict[str, str], changed: dict[str, str]) -> None:
    contract = _full_vocab_contract()
    base = contract.params_hash(contract.validate_and_coerce(override))
    other = contract.params_hash(contract.validate_and_coerce(changed))
    assert base != other


def test_type_binding_prevents_cross_type_collision() -> None:
    # "1" (string), 1 (int), and True (bool) must not collide.
    string_c = ParamContract.from_schema([{"name": "v", "type": "string"}])
    int_c = ParamContract.from_schema([{"name": "v", "type": "int"}])
    bool_c = ParamContract.from_schema([{"name": "v", "type": "bool"}])
    hs = string_c.params_hash(string_c.validate_and_coerce({"v": "1"}))
    hi = int_c.params_hash(int_c.validate_and_coerce({"v": "1"}))
    hb = bool_c.params_hash(bool_c.validate_and_coerce({"v": "1"}))
    assert len({hs, hi, hb}) == 3


@given(
    target=st.text(min_size=1, max_size=40).filter(lambda s: "\x00" not in s),
    retries=st.integers(min_value=-1000, max_value=1000),
    threshold=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    dry_run=st.booleans(),
)
def test_property_hash_deterministic_full_vocab(target: str, retries: int, threshold: float, dry_run: bool) -> None:
    contract = _full_vocab_contract()
    raw = {"target": target, "retries": str(retries), "threshold": repr(threshold), "dry_run": str(dry_run)}
    a = contract.validated_hash(raw)[1]
    b = contract.validated_hash(dict(raw))[1]
    assert a == b


# ---------------------------------------------------------------------------
# Validation + JSONPath diagnostics
# ---------------------------------------------------------------------------


def test_missing_required_carries_jsonpath() -> None:
    contract = _full_vocab_contract()
    with pytest.raises(ParamContractViolation) as exc:
        contract.validate_and_coerce({})
    assert exc.value.json_path == "$.params.target"
    assert exc.value.reason_code == "missing_required"
    assert exc.value.schema_hash.startswith("sha256:")


def test_unknown_param_carries_jsonpath_and_value_digest() -> None:
    contract = _full_vocab_contract()
    with pytest.raises(ParamContractViolation) as exc:
        contract.validate_and_coerce({"target": "svc", "bogus": "x"})
    assert exc.value.json_path == "$.params.bogus"
    assert exc.value.reason_code == "unknown_param"
    assert exc.value.value_digest.startswith("sha256:")


def test_bad_type_carries_jsonpath() -> None:
    contract = _full_vocab_contract()
    with pytest.raises(ParamContractViolation) as exc:
        contract.validate_and_coerce({"target": "svc", "retries": "abc"})
    assert exc.value.json_path == "$.params.retries"
    assert exc.value.reason_code == "bad_type"


def test_bad_choice_carries_jsonpath() -> None:
    contract = _full_vocab_contract()
    with pytest.raises(ParamContractViolation) as exc:
        contract.validate_and_coerce({"target": "svc", "mode": "reckless"})
    assert exc.value.json_path == "$.params.mode"
    assert exc.value.reason_code == "bad_choice"


def test_value_digest_never_contains_raw_value() -> None:
    contract = ParamContract.from_schema([{"name": "secret", "type": "int"}])
    with pytest.raises(ParamContractViolation) as exc:
        contract.validate_and_coerce({"secret": "hunter2-not-an-int"})
    assert "hunter2" not in exc.value.value_digest


# ---------------------------------------------------------------------------
# AC5 backward compatibility
# ---------------------------------------------------------------------------


def test_empty_contract_round_trips() -> None:
    for schema in (None, [], ()):
        contract = ParamContract.from_schema(schema)
        assert contract.is_empty
        validated = contract.validate_and_coerce({})
        assert validated == {}


def test_empty_contract_hash_is_stable() -> None:
    a = ParamContract.from_schema(None)
    b = ParamContract.from_schema([])
    assert a.params_hash({}) == b.params_hash({})
    # Empty-params hash is a fixed sentinel, distinct from a populated map.
    populated = _full_vocab_contract()
    assert a.params_hash({}) != populated.params_hash(populated.validate_and_coerce({"target": "x"}))


def test_schema_hash_changes_with_declaration() -> None:
    a = ParamContract.from_schema([{"name": "x", "type": "int"}])
    b = ParamContract.from_schema([{"name": "x", "type": "string"}])
    assert a.schema_hash() != b.schema_hash()


# ---------------------------------------------------------------------------
# Spec-level guards
# ---------------------------------------------------------------------------


def test_reserved_goal_name_rejected() -> None:
    with pytest.raises(ParamContractError):
        ParamSpec(name="goal")


def test_choices_only_for_string() -> None:
    with pytest.raises(ParamContractError):
        ParamSpec(name="n", type="int", choices=("1", "2"))


def test_duplicate_param_names_rejected() -> None:
    with pytest.raises(ParamContractError):
        ParamContract.from_schema([{"name": "x"}, {"name": "x"}])


def test_vocabulary_constant_matches_literal() -> None:
    assert set(PARAM_TYPE_VOCABULARY) == {"string", "int", "float", "bool"}
