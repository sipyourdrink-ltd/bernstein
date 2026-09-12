"""A probe is a declared record, and an unknown field survives a round trip.

Which attribute a discovery pass collects, and what it costs, took how long and
may assert, were implicit in code: `agent_discovery._RICH_DETECTOR_NAMES` named
functions and nothing carried a refresh interval, a timeout, a cost class or a
taint tag. Adding one attribute to what a probe reports was a code change and a
release (#5081, slice 1).

The load-bearing test here is the round trip. A probe file written by a newer
build carries fields this one does not interpret; dropping them on load would
silently rewrite an operator's file the next time anything saved it, and the
damage would show up as a probe that quietly stopped doing something.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.govern.probe import (
    CollectionMethod,
    CostClass,
    Probe,
    ProbeError,
    load_probe_set,
)

if TYPE_CHECKING:
    from pathlib import Path


def _declaration(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "adapter-version",
        "attribute": "adapter.version",
        "collection_method": "command",
        "refresh_interval_s": 900,
        "timeout_s": 5,
        "cost_class": "cheap",
        "taint_tags": ["host-derived"],
    }
    base.update(overrides)
    return base


def _write(directory: Path, name: str, payload: dict[str, Any]) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The named, load-bearing test
# ---------------------------------------------------------------------------


def test_probe_record_round_trips_unknown_fields() -> None:
    """A field this build does not recognise survives load then save unchanged.

    Fails on main: no probe record exists at all, so a newer build's field has
    nowhere to survive.
    """
    raw = _declaration(sampling_strategy="reservoir", future_budget={"tokens": 10})
    probe = Probe.from_dict(raw)
    assert probe.unknown == {"sampling_strategy": "reservoir", "future_budget": {"tokens": 10}}
    assert probe.to_dict() == raw


def test_a_round_trip_is_stable_across_two_passes() -> None:
    """Load-save-load-save must not drift, or a file rewrites itself forever."""
    raw = _declaration(unrecognised={"a": [1, 2]})
    once = Probe.from_dict(raw).to_dict()
    twice = Probe.from_dict(once).to_dict()
    assert once == twice == raw


def test_known_keys_serialize_in_a_fixed_order() -> None:
    """Two writers of one probe produce the same bytes."""
    first = json.dumps(Probe.from_dict(_declaration(z_extra=1, a_extra=2)).to_dict())
    second = json.dumps(Probe.from_dict(_declaration(a_extra=2, z_extra=1)).to_dict())
    assert first == second


# ---------------------------------------------------------------------------
# What a declaration must carry
# ---------------------------------------------------------------------------


def test_a_probe_declares_everything_the_issue_asks_for() -> None:
    probe = Probe.from_dict(_declaration())
    assert probe.id == "adapter-version"
    assert probe.attribute == "adapter.version"
    assert probe.collection_method is CollectionMethod.COMMAND
    assert probe.refresh_interval_s == 900.0
    assert probe.timeout_s == 5.0
    assert probe.cost_class is CostClass.CHEAP
    assert probe.taint_tags == ("host-derived",)


def test_a_probe_without_a_timeout_is_refused() -> None:
    """A probe with no ceiling is a probe that can hang a whole pass."""
    raw = _declaration()
    del raw["timeout_s"]
    with pytest.raises(ProbeError, match="timeout_s"):
        Probe.from_dict(raw)


@pytest.mark.parametrize("bad", [0, -1, "5", True])
def test_a_non_positive_timeout_is_refused(bad: object) -> None:
    """Zero is not a ceiling, and a bool is not a number."""
    with pytest.raises(ProbeError, match="timeout_s"):
        Probe.from_dict(_declaration(timeout_s=bad))


def test_an_unknown_collection_method_names_what_is_allowed() -> None:
    """A refusal an operator can act on without reading the source."""
    with pytest.raises(ProbeError, match="command"):
        Probe.from_dict(_declaration(collection_method="telepathy"))


def test_taint_tags_must_be_strings() -> None:
    with pytest.raises(ProbeError, match="taint_tags"):
        Probe.from_dict(_declaration(taint_tags=[{"nested": True}]))


# ---------------------------------------------------------------------------
# Loading a directory
# ---------------------------------------------------------------------------


def test_a_directory_of_declarations_loads_in_name_order(tmp_path: Path) -> None:
    """Sorted, so the set is the same on every machine."""
    _write(tmp_path, "b.json", _declaration(id="b", attribute="b"))
    _write(tmp_path, "a.json", _declaration(id="a", attribute="a"))
    probe_set = load_probe_set(tmp_path)
    assert [p.id for p in probe_set] == ["a", "b"]
    assert len(probe_set) == 2
    assert probe_set.by_id("b") is not None


def test_the_version_comes_from_the_set_not_from_a_hash(tmp_path: Path) -> None:
    """An operator who edits a probe without bumping is making a statement."""
    _write(tmp_path, "probe-set.json", {"version": "2026.09.1"})
    _write(tmp_path, "a.json", _declaration(id="a"))
    assert load_probe_set(tmp_path).version == "2026.09.1"


def test_two_probes_sharing_an_id_are_refused(tmp_path: Path) -> None:
    """The id is what a journal entry names; two of them make it unanswerable."""
    _write(tmp_path, "one.json", _declaration(id="dup"))
    _write(tmp_path, "two.json", _declaration(id="dup"))
    with pytest.raises(ProbeError, match="already declared"):
        load_probe_set(tmp_path)


def test_a_missing_directory_is_an_empty_set(tmp_path: Path) -> None:
    """An operator who declared no probes has declared no probes."""
    probe_set = load_probe_set(tmp_path / "absent")
    assert len(probe_set) == 0


def test_an_unparsable_file_names_itself(tmp_path: Path) -> None:
    """Fails where it is read, not halfway through a discovery pass."""
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ProbeError, match="broken.json"):
        load_probe_set(tmp_path)
