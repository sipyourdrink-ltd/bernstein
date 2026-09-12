"""Deep-collection adapter selection is a registry, not a dispatch branch.

`_RICH_DETECTOR_NAMES` mapped a registry name to the *name of a module-level
function*, and the dispatch loop resolved it through `globals()`. Adding an
entity class meant editing that table and adding a `_detect_*` function — a
code change and a release for something that is data, and a review surface
nine other detectors run through (#5081, slice 2).

Selection is now `(matcher, adapter)` pairs. The load-bearing test below plants
a class and asserts it is collected with no edit outside the registry.

Slice 2 only. The probe-record format, jitter and timeouts, and the per-run
journal entry are the other slices and are not here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.agents import agent_discovery
from bernstein.core.agents.agent_discovery import (
    DetectorRegistration,
    register_detector,
    resolve_detector,
    unregister_detector,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bernstein.core.agents.agent_discovery import AgentCapabilities


@pytest.fixture
def clean_registry() -> Iterator[list[DetectorRegistration]]:
    """Restore the registry, so a registration cannot leak between tests."""
    original = list(agent_discovery._DETECTOR_REGISTRY)
    yield original
    agent_discovery._DETECTOR_REGISTRY[:] = original


# ---------------------------------------------------------------------------
# The named, load-bearing test
# ---------------------------------------------------------------------------


def test_adapter_selection_resolves_new_class_with_zero_dispatch_changes(
    clean_registry: list[DetectorRegistration],
) -> None:
    """A planted class is collected without touching the dispatch loop.

    Fails on main: there is no registry, and the only way to reach deep
    collection is an entry in `_RICH_DETECTOR_NAMES` plus a new `_detect_*`
    function in this module.
    """
    collected: list[str] = []

    def _matches(name: str) -> bool:
        return name.startswith("planted-")

    def _collect(name: str) -> tuple[AgentCapabilities | None, list[str]]:
        collected.append(name)
        return None, [f"collected {name}"]

    register_detector(_matches, _collect, source="test:planted")

    registration = resolve_detector("planted-thing")
    assert registration is not None
    assert registration.source == "test:planted"

    agent, warnings = registration.adapter("planted-thing")
    assert agent is None
    assert warnings == ["collected planted-thing"]
    assert collected == ["planted-thing"]


# ---------------------------------------------------------------------------
# What the migration must not change
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["aider", "claude", "codex", "cursor", "gemini", "kilo", "kiro", "opencode", "qwen"],
)
def test_every_builtin_detector_still_resolves(name: str) -> None:
    """The nine that had a table entry are the nine that resolve."""
    registration = resolve_detector(name)
    assert registration is not None
    assert registration.source == f"builtin:{agent_discovery._RICH_DETECTOR_NAMES[name]}"


def test_an_entry_with_no_registration_falls_through_to_the_sweep() -> None:
    """The other half of the old branch: `_detect_registry_cli` still owns it."""
    assert resolve_detector("definitely-not-registered") is None


def test_a_builtin_detector_is_resolved_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the name-keyed table existed to preserve.

    The unit tests monkeypatch the individual `_detect_*` functions. If the
    registry captured the function object at import time the patch would be
    invisible, and the migration would quietly break every one of them.
    """
    sentinel = (None, ["patched claude"])
    monkeypatch.setattr(agent_discovery, "_detect_claude", lambda: sentinel)
    registration = resolve_detector("claude")
    assert registration is not None
    assert registration.adapter("claude") == sentinel


def test_a_builtin_claims_its_entity_before_a_later_registration(
    clean_registry: list[DetectorRegistration],
) -> None:
    """First match wins, so a third-party pair cannot take over `claude`."""
    register_detector(lambda name: True, lambda name: (None, ["hijacked"]), source="test:greedy")
    registration = resolve_detector("claude")
    assert registration is not None
    assert registration.source == "builtin:_detect_claude"


def test_a_matcher_that_raises_does_not_abort_resolution(
    clean_registry: list[DetectorRegistration],
) -> None:
    """One bad third-party pair must not fail the whole discovery pass."""

    def _explodes(name: str) -> bool:
        raise RuntimeError("bad matcher")

    agent_discovery._DETECTOR_REGISTRY.insert(
        0, DetectorRegistration(matcher=_explodes, adapter=lambda name: (None, []), source="test:bad")
    )
    registration = resolve_detector("claude")
    assert registration is not None
    assert registration.source == "builtin:_detect_claude"


def test_unregistering_removes_the_pair(clean_registry: list[DetectorRegistration]) -> None:
    """A registration can be withdrawn, and withdrawing twice is not an error."""
    registration = register_detector(lambda name: name == "ephemeral", lambda name: (None, []), source="test:ephemeral")
    assert resolve_detector("ephemeral") is not None
    unregister_detector(registration)
    assert resolve_detector("ephemeral") is None
    unregister_detector(registration)
