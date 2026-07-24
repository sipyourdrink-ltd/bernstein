"""A2A task-state mapping covers ``input-required`` and ``auth-required`` (#2609).

The binding design directive requires the A2A protocol states to map onto
Bernstein task states, explicitly including the two states a callable node
must be able to express: ``input-required`` (the node needs more from the
caller) and ``auth-required`` (the node needs the caller to authenticate to a
downstream resource before it can proceed).
"""

from __future__ import annotations

from bernstein.core.protocols.a2a.a2a import (
    A2AHandler,
    A2ATaskStatus,
)


def test_auth_required_state_exists() -> None:
    assert A2ATaskStatus.AUTH_REQUIRED.value == "auth-required"


def test_input_required_state_exists() -> None:
    assert A2ATaskStatus.INPUT_REQUIRED.value == "input-required"


def test_auth_required_maps_to_a_bernstein_blocked_state() -> None:
    # ``auth-required`` is a wait state: the node cannot progress until the
    # caller supplies downstream credentials, which is a blocked task locally.
    assert A2AHandler.bernstein_status_for(A2ATaskStatus.AUTH_REQUIRED) == "blocked"


def test_input_required_maps_to_blocked() -> None:
    assert A2AHandler.bernstein_status_for(A2ATaskStatus.INPUT_REQUIRED) == "blocked"


def test_every_a2a_state_has_a_bernstein_mapping() -> None:
    # A missing entry silently degrades to "open"; assert the table is total
    # so a newly added state fails loudly here instead of mis-projecting.
    for state in A2ATaskStatus:
        mapped = A2AHandler.bernstein_status_for(state)
        assert mapped, f"{state} has no Bernstein mapping"
