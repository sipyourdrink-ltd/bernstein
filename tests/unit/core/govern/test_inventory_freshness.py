"""Issue #5083: existence in the inventory has to mean re-observed recently.

`Inventory` was a static tuple assembled once. There was no `observed_at`, no
upsert, and nothing that ever removed or demoted an entry -- so `govern
discover`, which runs repeatedly against the same targets, made an entity
unplugged six months ago look identical to one seen five minutes ago, and a
second pass over the same environment grew a duplicate row for every surface.
"""

from __future__ import annotations

import pytest

from bernstein.core.govern.inventory_models import Inventory, Surface

HOUR = 3600.0
DAY = 24 * HOUR


def _surface(name: str, value: str = "private", *, at: float = 0.0) -> Surface:
    return Surface(surface=name, observed_value=value, evidence_ref=f"q-{name}", observed_at=at)


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def test_a_second_observation_replaces_rather_than_appends() -> None:
    """The duplicate-entity problem, which is the whole reason for a stable id."""
    inventory = Inventory(surfaces=()).upsert(_surface("bucket-a", at=1000.0))
    inventory = inventory.upsert(_surface("bucket-a", "public-read", at=2000.0))

    (only,) = inventory.surfaces
    assert only.observed_value == "public-read"
    assert only.observed_at == 2000.0


def test_upsert_replaces_in_place_and_appends_new_ones() -> None:
    """Position stability is what makes a repeated pass converge.

    Re-sorting would rewrite the order of an unchanged inventory the moment one
    new entity arrived.
    """
    inventory = Inventory(surfaces=(_surface("a", at=10.0), _surface("b", at=10.0), _surface("c", at=10.0)))
    inventory = inventory.upsert(_surface("b", "changed", at=20.0))
    inventory = inventory.upsert(_surface("d", at=20.0))

    assert [s.surface for s in inventory.surfaces] == ["a", "b", "c", "d"]
    assert inventory.surfaces[1].observed_value == "changed"


def test_two_overlapping_passes_converge_to_one_state() -> None:
    """Requirement 3: a scheduled discovery pass is safe to run against itself."""
    base = Inventory(surfaces=())
    observations = [_surface("a", at=100.0), _surface("b", at=100.0)]

    first = base
    for observation in observations:
        first = first.upsert(observation)
    second = first
    for observation in observations:
        second = second.upsert(observation)

    assert second.to_dict() == first.to_dict()
    assert second.content_hash() == first.content_hash()
    assert len(second.surfaces) == 2


def test_observed_at_reports_the_last_time_we_looked() -> None:
    inventory = Inventory(surfaces=()).upsert(_surface("a", at=1234.0))
    assert inventory.observed_at("a") == 1234.0
    assert inventory.observed_at("never-seen") is None


# ---------------------------------------------------------------------------
# The content hash
# ---------------------------------------------------------------------------


def test_re_observing_an_unchanged_surface_does_not_move_the_hash() -> None:
    """The hash is about the environment; the timestamp is about the observer."""
    early = Inventory(surfaces=(_surface("a", at=100.0),))
    later = early.upsert(_surface("a", at=999_999.0))

    assert later.content_hash() == early.content_hash()
    assert later.observed_at("a") == 999_999.0


def test_a_changed_value_does_move_the_hash() -> None:
    """The control: without it the test above passes on a hash of nothing."""
    before = Inventory(surfaces=(_surface("a", "private", at=100.0),))
    after = before.upsert(_surface("a", "public-read", at=100.0))
    assert after.content_hash() != before.content_hash()


def test_tombstones_do_not_move_the_hash() -> None:
    """The hash answers "what is out there"; a tombstone records what is not."""
    now = 10 * DAY
    inventory = Inventory(surfaces=(_surface("a", at=now), _surface("b", at=0.0)))
    swept = inventory.sweep(older_than=DAY, now=now).inventory
    assert swept.tombstones, "the fixture must actually tombstone something"

    live_only = Inventory(surfaces=(_surface("a", at=now),))
    assert swept.content_hash() == live_only.content_hash()


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_the_sweep_moves_what_it_has_not_seen_inside_the_window() -> None:
    now = 10 * DAY
    inventory = Inventory(
        surfaces=(
            _surface("fresh", at=now - HOUR),
            _surface("stale", at=now - 8 * DAY),
        )
    )

    result = inventory.sweep(older_than=DAY, now=now)

    assert result.moved == 1
    assert [s.surface for s in result.tombstoned] == ["stale"]
    assert [s.surface for s in result.inventory.surfaces] == ["fresh"]
    assert result.inventory.is_tombstoned("stale") is True


def test_nothing_is_hard_deleted() -> None:
    """ "When did we stop seeing X" is not answerable from a row that is gone."""
    now = 10 * DAY
    inventory = Inventory(surfaces=(_surface("gone", "public-read", at=now - 8 * DAY),))

    swept = inventory.sweep(older_than=DAY, now=now).inventory

    (tombstone,) = swept.tombstones
    assert tombstone.tombstoned_at == now
    # And what it looked like when we lost it, without a second store.
    assert tombstone.surface.observed_value == "public-read"
    assert tombstone.surface.observed_at == now - 8 * DAY


def test_a_sweep_that_moves_nothing_returns_the_same_inventory() -> None:
    """A no-op sweep must not rewrite a document for no reason."""
    now = 10 * DAY
    inventory = Inventory(surfaces=(_surface("fresh", at=now),))

    result = inventory.sweep(older_than=DAY, now=now)

    assert result.moved == 0
    assert result.inventory is inventory


def test_a_surface_observed_exactly_on_the_boundary_survives() -> None:
    """The window is inclusive: `older_than` seconds ago is not older than that."""
    now = 10 * DAY
    inventory = Inventory(surfaces=(_surface("edge", at=now - DAY),))
    assert inventory.sweep(older_than=DAY, now=now).moved == 0


def test_a_never_observed_surface_is_swept() -> None:
    """`observed_at` defaults to 0.0, which is the "never re-observed" reading."""
    inventory = Inventory(surfaces=(_surface("legacy"),))
    assert inventory.sweep(older_than=DAY, now=10 * DAY).moved == 1


def test_a_negative_window_is_refused() -> None:
    """It would tombstone everything, including what was just seen."""
    with pytest.raises(ValueError, match="older_than"):
        Inventory(surfaces=()).sweep(older_than=-1.0, now=0.0)


# ---------------------------------------------------------------------------
# Restoration
# ---------------------------------------------------------------------------


def test_a_tombstoned_entity_that_reappears_is_restored() -> None:
    now = 10 * DAY
    inventory = Inventory(surfaces=(_surface("flaky", at=now - 8 * DAY),))
    swept = inventory.sweep(older_than=DAY, now=now).inventory
    assert swept.is_tombstoned("flaky") is True

    restored = swept.upsert(_surface("flaky", at=now))

    assert restored.is_tombstoned("flaky") is False
    assert [s.surface for s in restored.surfaces] == ["flaky"]
    assert restored.observed_at("flaky") == now


def test_restoring_one_entity_leaves_other_tombstones_alone() -> None:
    now = 10 * DAY
    inventory = Inventory(surfaces=(_surface("a", at=now - 8 * DAY), _surface("b", at=now - 8 * DAY)))
    swept = inventory.sweep(older_than=DAY, now=now).inventory

    restored = swept.upsert(_surface("a", at=now))

    assert restored.is_tombstoned("a") is False
    assert restored.is_tombstoned("b") is True


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_the_tombstone_partition_round_trips() -> None:
    now = 10 * DAY
    inventory = Inventory(surfaces=(_surface("a", at=now), _surface("b", at=0.0)))
    swept = inventory.sweep(older_than=DAY, now=now).inventory

    assert Inventory.from_dict(swept.to_dict()) == swept


def test_a_document_written_before_this_change_still_loads() -> None:
    """No `observed_at`, no `tombstones` -- and it reads as never re-observed."""
    legacy = {"surfaces": [{"surface": "a", "observed_value": "private", "evidence_ref": "q1"}]}

    inventory = Inventory.from_dict(legacy)

    assert inventory.observed_at("a") == 0.0
    assert inventory.tombstones == ()
    assert inventory.sweep(older_than=DAY, now=10 * DAY).moved == 1
