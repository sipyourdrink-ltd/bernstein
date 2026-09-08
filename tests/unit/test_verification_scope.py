"""``VerificationScope``: fields, validation and canonical serialisation (#5395).

A gate verdict is only worth what the evidence behind it covers.
``VerificationScope`` is the value that states that coverage, so the type has
to hold two properties: it must round-trip without losing a field, and two
equal scopes must serialise to identical bytes so a scope can be folded into
a hashed subject later without a second canonical form appearing in the
codebase.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.quality.gate_pipeline import (
    SCOPE_CONFIDENCE_LEVELS,
    GateResult,
    VerificationScope,
)


def _scope(**overrides: object) -> VerificationScope:
    """A fully populated scope, so no test depends on a default it forgot."""
    kwargs: dict[str, object] = {
        "oracle_id": "ruff",
        "kind": "lint",
        "checked": ("src/a.py", "src/b.py"),
        "cannot_check": ("src/generated_pb2.py",),
        "confidence": "high",
        "evidence_ref": "sha256:0f1e",
    }
    kwargs.update(overrides)
    return VerificationScope(**kwargs)  # type: ignore[arg-type]


def test_scope_carries_every_declared_field() -> None:
    """The six fields the type promises are all present and readable."""
    scope = _scope()
    assert scope.oracle_id == "ruff"
    assert scope.kind == "lint"
    assert scope.checked == ("src/a.py", "src/b.py")
    assert scope.cannot_check == ("src/generated_pb2.py",)
    assert scope.confidence == "high"
    assert scope.evidence_ref == "sha256:0f1e"


def test_dict_round_trip_preserves_every_field() -> None:
    """``from_dict(to_dict(x)) == x``, tuples included.

    Sequences must come back as tuples: a scope whose ``checked`` is a list
    would compare unequal to the one it was built from, and a frozen
    dataclass holding a list is not hashable.
    """
    scope = _scope()
    restored = VerificationScope.from_dict(scope.to_dict())
    assert restored == scope
    assert isinstance(restored.checked, tuple)
    assert isinstance(restored.cannot_check, tuple)


def test_round_trip_survives_real_json() -> None:
    """The dict form is JSON-serialisable, not merely dict-shaped."""
    scope = _scope()
    assert VerificationScope.from_dict(json.loads(json.dumps(scope.to_dict()))) == scope


def test_optional_identity_fields_round_trip_as_none() -> None:
    """A gate with no oracle identity keeps ``None``, not ``""``."""
    scope = _scope(oracle_id=None, kind=None)
    assert VerificationScope.from_dict(scope.to_dict()) == scope


def test_equal_scopes_serialise_to_identical_bytes() -> None:
    """Byte-stability is the whole point: two equal scopes, one digest."""
    assert _scope().canonical_bytes() == _scope().canonical_bytes()


def test_serialisation_key_order_does_not_depend_on_construction_order() -> None:
    """Keys are sorted, so a scope built field-by-field in any order matches.

    Constructed with keywords in reverse declaration order; the bytes must be
    the ones the forward-order construction produces.
    """
    reversed_order = VerificationScope(
        evidence_ref="sha256:0f1e",
        confidence="high",
        cannot_check=("src/generated_pb2.py",),
        checked=("src/a.py", "src/b.py"),
        kind="lint",
        oracle_id="ruff",
    )
    assert reversed_order.canonical_bytes() == _scope().canonical_bytes()


def test_canonical_bytes_are_sorted_and_compact() -> None:
    """The convention is the receipt family's: sorted keys, no whitespace."""
    raw = _scope().canonical_bytes()
    assert b", " not in raw and b": " not in raw
    assert list(json.loads(raw)) == sorted(json.loads(raw))


def test_differing_scopes_serialise_differently() -> None:
    """A blind spot moving into the checked set must change the bytes."""
    a = _scope(cannot_check=())
    b = _scope(checked=("src/a.py", "src/b.py", "src/generated_pb2.py"), cannot_check=())
    assert a.canonical_bytes() != b.canonical_bytes()


@pytest.mark.parametrize("bad", ["medium", "HIGH", "", "unknown"])
def test_confidence_outside_the_closed_set_raises_at_construction(bad: str) -> None:
    """The message names the offending value and the allowed set."""
    with pytest.raises(ValueError) as excinfo:
        _scope(confidence=bad)
    message = str(excinfo.value)
    assert repr(bad) in message
    for level in SCOPE_CONFIDENCE_LEVELS:
        assert level in message


@pytest.mark.parametrize("level", sorted(SCOPE_CONFIDENCE_LEVELS))
def test_every_declared_confidence_level_is_constructible(level: str) -> None:
    """The closed set and the validator cannot disagree about a member."""
    assert _scope(confidence=level).confidence == level


@pytest.mark.parametrize("field_name", ["checked", "cannot_check"])
def test_a_bare_string_is_rejected_rather_than_iterated(field_name: str) -> None:
    """``checked="a.py"`` must not silently become five one-character paths."""
    with pytest.raises(TypeError) as excinfo:
        _scope(**{field_name: "src/a.py"})
    assert field_name in str(excinfo.value)


def test_gate_result_accepts_and_preserves_a_scope() -> None:
    """The field is carried through unchanged, and still defaults to None."""
    scope = _scope()
    result = GateResult(
        name="lint",
        status="pass",
        required=True,
        blocked=False,
        cached=False,
        duration_ms=12,
        details="",
        scope=scope,
    )
    assert result.scope == scope
    assert (
        GateResult(
            name="lint",
            status="pass",
            required=True,
            blocked=False,
            cached=False,
            duration_ms=12,
            details="",
        ).scope
        is None
    )
