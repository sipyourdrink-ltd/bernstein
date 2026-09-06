"""Issue #5120: reconciliation lanes are data, and bootstrap is create-if-absent.

There was no "lane" concept anywhere. The pattern this needs -- a named,
content-hashed record where re-registering an unchanged one is a no-op --
already existed for sandbox pools (``pool register``'s
``action = "unchanged"``), and lanes are the missing half of it.

The barrier is why a lane is one mechanism rather than two: a canary lane that
must serialize its steps and a bulk lane where one stuck target must not block
the rest are the same runner with one flag flipped.
"""

from __future__ import annotations

import pytest

from bernstein.core.govern.lanes import (
    Barrier,
    LaneAction,
    LaneError,
    LaneManifest,
    load_lane_set,
    reconcile_lanes,
)


def _lane(name: str = "canary", **over: object) -> LaneManifest:
    fields: dict[str, object] = {
        "name": name,
        "selector": "env=prod,tier=1",
        "schedule": "0 * * * *",
        "log_destination": f".sdd/logs/{name}",
    }
    fields.update(over)
    return LaneManifest(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_a_lane_carries_every_field_the_issue_names() -> None:
    lane = _lane(timeout_seconds=900, barrier=Barrier.PER_STEP)
    assert lane.name == "canary"
    assert lane.selector == "env=prod,tier=1"
    assert lane.schedule == "0 * * * *"
    assert lane.timeout_seconds == 900
    assert lane.log_destination == ".sdd/logs/canary"
    assert lane.barrier is Barrier.PER_STEP


def test_the_barrier_defaults_to_free() -> None:
    """The bulk case is the common one; a canary opts in to serializing."""
    assert _lane().barrier is Barrier.FREE


def test_the_hash_is_the_identity_and_moves_with_any_field() -> None:
    base = _lane()
    for changed in (
        {"selector": "env=staging"},
        {"schedule": "0 3 * * *"},
        {"timeout_seconds": 60},
        {"log_destination": ".sdd/logs/other"},
        {"barrier": Barrier.PER_STEP},
    ):
        assert _lane(**changed).lane_hash != base.lane_hash, changed


def test_two_lanes_declared_identically_hash_identically() -> None:
    assert _lane().lane_hash == _lane().lane_hash


def test_the_hash_is_computed_not_accepted() -> None:
    """A hash somebody can pass in is a hash somebody can pass in wrong."""
    lane = LaneManifest(
        name="canary",
        selector="env=prod",
        schedule="0 * * * *",
        log_destination=".sdd/logs/canary",
        lane_hash="deadbeef",
    )
    assert lane.lane_hash != "deadbeef"


@pytest.mark.parametrize("field_name", ["name", "selector", "schedule", "log_destination"])
def test_an_empty_required_field_is_refused(field_name: str) -> None:
    with pytest.raises(LaneError, match=field_name):
        _lane(**{field_name: "   "})


def test_a_negative_timeout_is_refused() -> None:
    with pytest.raises(LaneError, match="timeout_seconds"):
        _lane(timeout_seconds=-1)


def test_a_zero_timeout_means_no_ceiling() -> None:
    """Matching the convention PoolManifest.max_concurrency already uses."""
    assert _lane(timeout_seconds=0).timeout_seconds == 0


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_a_lane_round_trips() -> None:
    lane = _lane(barrier=Barrier.PER_STEP, timeout_seconds=30)
    assert LaneManifest.from_dict(lane.to_dict()) == lane


def test_a_declared_hash_is_verified_not_trusted() -> None:
    """Trusting it would let an edited lane keep the identity that was reviewed."""
    document = _lane().to_dict()
    document["selector"] = "env=everything"

    with pytest.raises(LaneError, match="edited after it was hashed"):
        LaneManifest.from_dict(document)


def test_a_document_with_no_hash_is_accepted_and_hashed() -> None:
    document = _lane().to_dict()
    del document["lane_hash"]
    assert LaneManifest.from_dict(document).lane_hash == _lane().lane_hash


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(LaneError, match="unknown key"):
        LaneManifest.from_dict({**_lane().to_dict(), "barier": "free"})


def test_an_unknown_barrier_is_refused_and_names_the_known_ones() -> None:
    document = _lane().to_dict()
    document["barrier"] = "sequential"
    del document["lane_hash"]
    with pytest.raises(LaneError, match="not one of") as excinfo:
        LaneManifest.from_dict(document)
    assert "per-step" in str(excinfo.value)
    assert "free" in str(excinfo.value)


def test_a_non_integer_timeout_is_refused() -> None:
    document = _lane().to_dict()
    document["timeout_seconds"] = "soon"
    del document["lane_hash"]
    with pytest.raises(LaneError, match="not an integer"):
        LaneManifest.from_dict(document)


def test_a_lane_set_document_loads() -> None:
    lanes = load_lane_set({"lanes": [_lane("canary").to_dict(), _lane("bulk").to_dict()]})
    assert [lane.name for lane in lanes] == ["canary", "bulk"]


def test_a_lane_set_with_an_unknown_key_is_refused() -> None:
    with pytest.raises(LaneError, match="unknown key"):
        load_lane_set({"lanes": [], "lanez": []})


def test_a_lane_set_whose_lanes_are_not_a_list_is_refused() -> None:
    with pytest.raises(LaneError, match="must be a list"):
        load_lane_set({"lanes": {"name": "canary"}})


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_a_first_bootstrap_registers_everything() -> None:
    result = reconcile_lanes([_lane("canary"), _lane("bulk")])

    assert [e.action for e in result.entries] == [LaneAction.REGISTERED, LaneAction.REGISTERED]
    assert result.is_noop is False


def test_running_it_twice_against_an_unchanged_set_is_a_no_op() -> None:
    """The issue's named test. Bootstrap must change nothing AND say so."""
    lanes = [_lane("canary"), _lane("bulk")]
    first = reconcile_lanes(lanes)
    existing = {e.lane.name: e.lane.lane_hash for e in first.entries}

    second = reconcile_lanes(lanes, existing)

    assert [e.action for e in second.entries] == [LaneAction.UNCHANGED, LaneAction.UNCHANGED]
    assert second.is_noop is True
    assert second.changed == ()


def test_only_the_lane_that_moved_is_updated() -> None:
    existing = {
        "canary": _lane("canary").lane_hash,
        "bulk": _lane("bulk").lane_hash,
    }

    result = reconcile_lanes([_lane("canary"), _lane("bulk", schedule="0 3 * * *")], existing)

    assert [e.action for e in result.entries] == [LaneAction.UNCHANGED, LaneAction.UPDATED]
    assert result.is_noop is False
    assert [e.lane.name for e in result.changed] == ["bulk"]


def test_an_updated_lane_reports_the_hash_it_replaced() -> None:
    previous = _lane("bulk").lane_hash
    result = reconcile_lanes([_lane("bulk", timeout_seconds=60)], {"bulk": previous})
    assert result.entries[0].prev_hash == previous


def test_declared_order_is_preserved_so_a_report_reads_like_the_file() -> None:
    names = ["zulu", "alpha", "mike"]
    result = reconcile_lanes([_lane(name) for name in names])
    assert [e.lane.name for e in result.entries] == names


def test_a_lane_missing_from_the_file_is_not_retired_here() -> None:
    """A retirement is a deliberate decision, not one inferred from an omission."""
    existing = {"canary": _lane("canary").lane_hash, "retired": "abc"}
    result = reconcile_lanes([_lane("canary")], existing)

    assert len(result.entries) == 1
    assert result.is_noop is True


def test_two_declared_lanes_cannot_share_a_name() -> None:
    """The set is keyed on it, so the second would silently shadow the first."""
    with pytest.raises(LaneError, match="two declared lanes"):
        reconcile_lanes([_lane("canary"), _lane("canary", schedule="0 3 * * *")])


def test_the_json_shape_mirrors_pool_register() -> None:
    document = reconcile_lanes([_lane("canary")]).to_dict()

    assert document["noop"] is False
    assert document["changed"] == 1
    row = document["lanes"][0]
    assert row["action"] == "registered"
    assert row["name"] == "canary"
    assert row["barrier"] == "free"
    assert len(row["lane_hash"]) == 64
