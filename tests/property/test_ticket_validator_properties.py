"""Property-based tests for ``bernstein.sdd.validator``.

We use Hypothesis to bash the schema with a wide range of inputs:

- Slug-pattern boundaries (length, charset).
- ISO date round-trip vs malformed dates.
- Enum exhaustiveness (any non-enum string is rejected; every enum value
  is accepted).
- Strict mode invariants: report contains no warnings when strict=True.
- Determinism: validating the same payload twice yields the same outcome.
"""

from __future__ import annotations

import datetime as _dt
import re
import string
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from bernstein.sdd.validator import (
    RECOMMENDED_KEYS,
    load_schema,
    validate_ticket_metadata,
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
_STATUSES = (
    "open",
    "claimed",
    "in_progress",
    "blocked",
    "closed",
    "closed_hit",
    "closed_miss",
    "closed_partial",
    "deduped",
    "superseded",
)
_PRIORITIES = ("P0", "P1", "P2")
_EFFORTS = ("S", "M", "L")
_TICKET_FIELDS = frozenset(load_schema()["properties"])


def _base() -> dict[str, Any]:
    return {
        "id": "feat-baseline-id",
        "created": "2026-05-17",
        "status": "open",
        "priority": "P1",
        "effort": "M",
    }


@st.composite
def _valid_id(draw: Any) -> str:
    head = draw(st.sampled_from(list(string.ascii_lowercase + string.digits)))
    body_len = draw(st.integers(min_value=2, max_value=20))
    body = draw(
        st.text(
            alphabet=st.sampled_from(list(string.ascii_lowercase + string.digits + "-")),
            min_size=body_len,
            max_size=body_len,
        )
    )
    return head + body


@given(slug=_valid_id())
def test_property_valid_slug_pattern_accepted(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        # Filter out anything that drifted out of pattern by chance.
        return
    meta = _base()
    meta["id"] = slug
    rep = validate_ticket_metadata(meta)
    assert rep.ok, [e.render() for e in rep.errors]


@given(slug=st.text(min_size=0, max_size=4))
def test_property_short_slugs_rejected(slug: str) -> None:
    if _SLUG_RE.match(slug):
        return
    meta = _base()
    meta["id"] = slug
    rep = validate_ticket_metadata(meta)
    assert not rep.ok


@given(status=st.sampled_from(_STATUSES))
def test_property_all_enum_statuses_accepted(status: str) -> None:
    meta = _base()
    meta["status"] = status
    rep = validate_ticket_metadata(meta)
    assert rep.ok


@given(status=st.text(min_size=1, max_size=30))
def test_property_arbitrary_status_strings_rejected_when_not_in_enum(status: str) -> None:
    if status in _STATUSES:
        return
    meta = _base()
    meta["status"] = status
    rep = validate_ticket_metadata(meta)
    assert not rep.ok


@given(priority=st.sampled_from(_PRIORITIES), effort=st.sampled_from(_EFFORTS))
def test_property_priority_effort_combinations(priority: str, effort: str) -> None:
    meta = _base()
    meta["priority"] = priority
    meta["effort"] = effort
    rep = validate_ticket_metadata(meta)
    assert rep.ok


@given(date_obj=st.dates(min_value=_dt.date(1970, 1, 1), max_value=_dt.date(2099, 12, 31)))
def test_property_iso_dates_accepted(date_obj: _dt.date) -> None:
    meta = _base()
    meta["created"] = date_obj.isoformat()
    rep = validate_ticket_metadata(meta)
    assert rep.ok


@given(bad=st.text(min_size=1, max_size=15))
def test_property_non_iso_dates_rejected(bad: str) -> None:
    try:
        _dt.date.fromisoformat(bad)
    except ValueError:
        meta = _base()
        meta["created"] = bad
        rep = validate_ticket_metadata(meta)
        assert not rep.ok


@given(
    extra=st.dictionaries(
        keys=st.text(
            alphabet=st.sampled_from(list(string.ascii_lowercase + "_")),
            min_size=1,
            max_size=10,
        ).filter(lambda k: k not in _TICKET_FIELDS),
        values=st.one_of(st.text(max_size=10), st.integers(), st.booleans(), st.none()),
        max_size=5,
    )
)
def test_property_extra_keys_do_not_break_validation(extra: dict[str, Any]) -> None:
    meta = _base() | extra
    rep = validate_ticket_metadata(meta)
    assert rep.ok


@given(missing=st.sampled_from(["id", "created", "status", "priority", "effort"]))
def test_property_each_required_key_removed_causes_failure(missing: str) -> None:
    meta = _base()
    del meta[missing]
    rep = validate_ticket_metadata(meta)
    assert not rep.ok
    assert any(missing in e.message for e in rep.errors)


@given(window_days=st.integers(min_value=-10, max_value=0))
def test_property_success_metric_nonpositive_window_rejected(window_days: int) -> None:
    meta = _base()
    meta["success_metric"] = {
        "name": "x",
        "current": 1,
        "target": 2,
        "window_days": window_days,
    }
    rep = validate_ticket_metadata(meta)
    assert not rep.ok


@given(confidence=st.floats(min_value=1.0001, max_value=10.0, allow_nan=False, allow_infinity=False))
def test_property_rice_confidence_above_one_rejected(confidence: float) -> None:
    meta = _base()
    meta["rice"] = {"confidence": confidence}
    rep = validate_ticket_metadata(meta)
    assert not rep.ok


@given(
    strict=st.booleans(),
)
def test_property_strict_invariant_no_warnings(strict: bool) -> None:
    rep = validate_ticket_metadata(_base(), strict=strict)
    if strict:
        # Strict mode never emits warnings - everything is an error or nothing.
        assert rep.warnings == []
    else:
        # Default mode emits a warning for every missing recommended key.
        assert len(rep.warnings) == len(RECOMMENDED_KEYS)


@given(payload=st.dictionaries(st.text(max_size=10), st.text(max_size=10), max_size=10))
def test_property_validate_metadata_is_deterministic(payload: dict[str, str]) -> None:
    rep_a = validate_ticket_metadata(payload)
    rep_b = validate_ticket_metadata(payload)
    assert rep_a.ok == rep_b.ok
    assert len(rep_a.errors) == len(rep_b.errors)
