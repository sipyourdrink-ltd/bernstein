"""Issue #5118: which governed targets lack our agent, as a live query.

Nothing answered that question -- not because it is hard, but because no probe
recorded presence and no attribute existed for a selector to ask through. A gap
list that is a query against live data never goes stale the way a hand-maintained
tracking sheet does.
"""

from __future__ import annotations

import pytest

from bernstein.core.govern.agent_presence import (
    AGENT_PRESENT_KEY,
    AGENT_VERSION_KEY,
    ENROLLMENT_KEY,
    ENROLLMENT_REASON_KEY,
    MISSING_AGENT_SELECTOR,
    PRESENT_FALSE,
    PRESENT_TRUE,
    UNENROLLABLE_SELECTOR,
    AgentPresence,
    Enrollment,
    apply_presence,
    enrollment_gap,
)
from bernstein.core.govern.selector import InventoryNode, InventoryStore, resolve_targets


def _store(*nodes: InventoryNode) -> InventoryStore:
    return InventoryStore(nodes=tuple(nodes))


def _probed(node_id: str, presence: AgentPresence, **attrs: tuple[str, ...]) -> InventoryNode:
    return apply_presence(InventoryNode(node_id, dict(attrs)), presence)


# ---------------------------------------------------------------------------
# The probe result
# ---------------------------------------------------------------------------


def test_an_enrolled_target_records_presence_and_version() -> None:
    node = _probed("a", AgentPresence.enrolled("a", "1.2.3"))

    assert node.attributes[AGENT_PRESENT_KEY] == (PRESENT_TRUE,)
    assert node.attributes[AGENT_VERSION_KEY] == ("1.2.3",)
    assert node.attributes[ENROLLMENT_KEY] == (Enrollment.ENROLLED.value,)


def test_an_agent_that_named_no_version_writes_no_version() -> None:
    """Empty is honest; `unknown` would be a claim the probe cannot make."""
    node = _probed("a", AgentPresence.enrolled("a"))

    assert node.attributes[AGENT_PRESENT_KEY] == (PRESENT_TRUE,)
    assert AGENT_VERSION_KEY not in node.attributes


def test_a_missing_agent_records_absence_with_no_reason() -> None:
    node = _probed("b", AgentPresence.missing("b"))

    assert node.attributes[AGENT_PRESENT_KEY] == (PRESENT_FALSE,)
    assert node.attributes[ENROLLMENT_KEY] == (Enrollment.MISSING.value,)
    # An empty attribute would match `enrollment_reason` queries and report a
    # reason nobody gave.
    assert ENROLLMENT_REASON_KEY not in node.attributes


def test_an_unenrollable_target_records_why() -> None:
    node = _probed("c", AgentPresence.unenrollable("c", "no systemd on this host"))

    assert node.attributes[ENROLLMENT_KEY] == (Enrollment.UNENROLLABLE.value,)
    assert node.attributes[ENROLLMENT_REASON_KEY] == ("no systemd on this host",)


def test_an_unenrollable_target_must_give_a_reason() -> None:
    """Without one it is indistinguishable from a target nobody has got to yet."""
    with pytest.raises(ValueError, match="record why"):
        AgentPresence.unenrollable("c", "   ")


# ---------------------------------------------------------------------------
# Applying it to a node
# ---------------------------------------------------------------------------


def test_applying_a_result_keeps_every_other_attribute() -> None:
    node = _probed("a", AgentPresence.enrolled("a", "1.0.0"), region=("us-east",), kind=("host",))

    assert node.attributes["region"] == ("us-east",)
    assert node.attributes["kind"] == ("host",)


def test_applying_a_result_keeps_group_edges() -> None:
    node = apply_presence(InventoryNode("a", {}, ("prod",)), AgentPresence.enrolled("a"))
    assert node.groups == ("prod",)


def test_a_later_probe_replaces_the_earlier_one() -> None:
    """A probe result is the current reading.

    Merging would leave a node claiming two versions, or claiming both a reason
    and an enrollment that no longer needs one.
    """
    first = _probed("a", AgentPresence.unenrollable("a", "no systemd"))
    second = apply_presence(first, AgentPresence.enrolled("a", "2.0.0"))

    assert second.attributes[ENROLLMENT_KEY] == (Enrollment.ENROLLED.value,)
    assert second.attributes[AGENT_VERSION_KEY] == ("2.0.0",)
    assert ENROLLMENT_REASON_KEY not in second.attributes


# ---------------------------------------------------------------------------
# The gap query
# ---------------------------------------------------------------------------


def test_gap_query_lists_targets_missing_presence() -> None:
    """The issue's named test."""
    store = _store(
        _probed("a", AgentPresence.enrolled("a", "1.2.3")),
        _probed("b", AgentPresence.missing("b")),
        _probed("c", AgentPresence.unenrollable("c", "no systemd")),
    )

    assert [node.node_id for node in enrollment_gap(store)] == ["b", "c"]


def test_the_two_kinds_of_gap_stay_distinguishable() -> None:
    """Collapsing them is what makes a gap list unactionable.

    Somebody works through it and re-tries targets that already refused.
    """
    store = _store(
        _probed("b", AgentPresence.missing("b")),
        _probed("c", AgentPresence.unenrollable("c", "no systemd")),
    )

    assert [n.node_id for n in resolve_targets(store, MISSING_AGENT_SELECTOR)] == ["b"]
    assert [n.node_id for n in resolve_targets(store, UNENROLLABLE_SELECTOR)] == ["c"]


def test_an_unprobed_target_is_not_a_gap() -> None:
    """ "We have not looked" is not "the agent is missing".

    Reporting it would put unprobed targets on a list of work nobody can do
    until a probe runs.
    """
    store = _store(InventoryNode("never-probed"), _probed("b", AgentPresence.missing("b")))

    assert [node.node_id for node in enrollment_gap(store)] == ["b"]


def test_the_gap_is_ordered_by_node_id() -> None:
    """Two operators running it get byte-identical output."""
    store = _store(
        _probed("zulu", AgentPresence.missing("zulu")),
        _probed("alpha", AgentPresence.missing("alpha")),
        _probed("mike", AgentPresence.missing("mike")),
    )

    assert [node.node_id for node in enrollment_gap(store)] == ["alpha", "mike", "zulu"]


def test_an_enrolled_target_never_appears() -> None:
    store = _store(_probed("a", AgentPresence.enrolled("a", "1.0.0")))
    assert enrollment_gap(store) == ()


def test_the_gap_composes_with_the_ordinary_grammar() -> None:
    """The point of writing presence as attributes: an operator can ask their own question."""
    from bernstein.core.govern.selector import Selector

    store = _store(
        _probed("a", AgentPresence.missing("a"), region=("us-east",)),
        _probed("b", AgentPresence.missing("b"), region=("eu-west",)),
    )

    selector = Selector.parse([ENROLLMENT_KEY, Enrollment.MISSING.value, "region", "us-east"])
    assert [node.node_id for node in resolve_targets(store, selector)] == ["a"]


def test_group_inherited_attributes_still_resolve_alongside_presence() -> None:
    from bernstein.core.govern.selector import InventoryGroup, Selector

    store = InventoryStore(
        nodes=(apply_presence(InventoryNode("a", {}, ("prod",)), AgentPresence.missing("a")),),
        groups=(InventoryGroup("prod", {"tier": ("1",)}),),
    )

    selector = Selector.parse([ENROLLMENT_KEY, Enrollment.MISSING.value, "tier", "1"])
    assert [node.node_id for node in resolve_targets(store, selector)] == ["a"]
