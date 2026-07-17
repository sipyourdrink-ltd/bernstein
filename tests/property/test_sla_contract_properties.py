"""Hypothesis property tests for SLA contract hashing (#2549).

Exercises the AC "contract hash determinism": registering the same contract body
yields the identical ``contract_hash``, and changing any semantic field changes
the hash.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from bernstein.core.planning.sla_store import (
    SUBJECT_ENVELOPE,
    SUBJECT_SCHEDULE,
    SUBJECT_TASK_FAMILY,
    build_contract,
    contract_from_dict,
)

_SUBJECTS = st.sampled_from([SUBJECT_SCHEDULE, SUBJECT_TASK_FAMILY, SUBJECT_ENVELOPE])
_SUBJECT_ID = st.text(alphabet="abcdefghijklmnop_0123456789", min_size=1, max_size=16)
_SECS = st.integers(min_value=0, max_value=1_000_000)
_RATE = st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False)


def _kwargs(subject_type: str, subject_id: str, a: int, b: int, c: int, rate: float) -> dict[str, object]:
    # Guarantee at least one declared axis so the contract is buildable.
    return {
        "subject_type": subject_type,
        "subject_id": subject_id or "s",
        "max_run_duration_s": a + 1,
        "start_lateness_s": b,
        "fire_frequency_s": c,
        "spend_rate_usd_per_hour": rate,
    }


@given(_SUBJECTS, _SUBJECT_ID, _SECS, _SECS, _SECS, _RATE)
def test_contract_hash_is_stable(subject_type: str, subject_id: str, a: int, b: int, c: int, rate: float) -> None:
    """Two builds of the same body land on the identical id and hash."""
    kwargs = _kwargs(subject_type, subject_id, a, b, c, rate)
    one = build_contract(**kwargs)  # type: ignore[arg-type]
    two = build_contract(**kwargs)  # type: ignore[arg-type]
    assert one.contract_hash == two.contract_hash
    assert one.id == two.id
    assert one.id == f"sla_{one.contract_hash[:12]}"


@given(_SUBJECTS, _SUBJECT_ID, _SECS, _SECS, _SECS, _RATE)
def test_changing_a_field_changes_the_hash(
    subject_type: str, subject_id: str, a: int, b: int, c: int, rate: float
) -> None:
    """Bumping any semantic field changes the content hash."""
    kwargs = _kwargs(subject_type, subject_id, a, b, c, rate)
    base = build_contract(**kwargs)  # type: ignore[arg-type]
    mutated = build_contract(**{**kwargs, "max_run_duration_s": a + 2})  # type: ignore[arg-type]
    assert base.contract_hash != mutated.contract_hash


@given(_SUBJECTS, _SUBJECT_ID, _SECS, _SECS, _SECS, _RATE)
def test_roundtrip_preserves_hash(subject_type: str, subject_id: str, a: int, b: int, c: int, rate: float) -> None:
    """A dict round-trip recomputes the identical hash (encoding invariance)."""
    kwargs = _kwargs(subject_type, subject_id, a, b, c, rate)
    base = build_contract(**kwargs)  # type: ignore[arg-type]
    rebuilt = contract_from_dict(base.to_dict())
    assert rebuilt.contract_hash == base.contract_hash
    assert rebuilt.id == base.id


@given(_SUBJECTS, _SUBJECT_ID, _SECS, _SECS, _SECS, _RATE)
def test_created_at_is_not_semantic(subject_type: str, subject_id: str, a: int, b: int, c: int, rate: float) -> None:
    """``created_at`` is bookkeeping and must not enter the hash."""
    kwargs = _kwargs(subject_type, subject_id, a, b, c, rate)
    early = build_contract(created_at=1.0, **kwargs)  # type: ignore[arg-type]
    late = build_contract(created_at=999.0, **kwargs)  # type: ignore[arg-type]
    assert early.contract_hash == late.contract_hash
