"""The ``session_state`` axis on :class:`AdapterStrategy` (issue #4288).

The axis exists so an adapter can declare that it reaches agent-side state
outliving a single spawn. Every other axis says how Bernstein *drives* a run;
this one says what the run leaves behind, which is what decides whether a
replay of the same inputs can be expected to reproduce the same outputs.

These pin the two properties the addition has to hold: every existing row
keeps its meaning (the default is stateless), and the axis reaches the
operator-facing view rather than being declared and then dropped.
"""

from __future__ import annotations

import pytest

from bernstein.adapters._contract import (
    DEFAULT_ADAPTER_STRATEGY,
    STRATEGY_MATRIX,
    AdapterStrategy,
    SessionState,
)


def test_every_declared_row_resolves_to_a_valid_session_state() -> None:
    """The acceptance criterion: no row can carry an unrecognised value."""
    assert STRATEGY_MATRIX, "STRATEGY_MATRIX is empty; this guard would check nothing"

    for name, strategy in STRATEGY_MATRIX.items():
        assert isinstance(strategy.session_state, SessionState), (
            f"{name} declares a session_state that is not a SessionState member"
        )


def test_default_is_stateless_so_existing_rows_keep_their_meaning() -> None:
    assert AdapterStrategy().session_state is SessionState.STATELESS
    assert DEFAULT_ADAPTER_STRATEGY.session_state is SessionState.STATELESS


def test_no_shipped_adapter_silently_became_persistent() -> None:
    # Every row was written before this axis existed, so all of them must still
    # read as stateless. Declaring letta_code is a separate issue under #3670.
    persistent = [
        name for name, strategy in STRATEGY_MATRIX.items() if strategy.session_state is not SessionState.STATELESS
    ]
    assert persistent == [], f"unexpected non-stateless declarations: {persistent}"


def test_axis_reaches_the_operator_facing_view() -> None:
    view = AdapterStrategy(session_state=SessionState.PERSISTENT_AGENT).to_dict()

    assert view["session_state"] == "persistent-agent"
    # The other four axes are still present and unchanged in shape.
    assert set(view) == {
        "resume",
        "dangerous_mode",
        "event_channel",
        "output_mode",
        "session_state",
    }


@pytest.mark.parametrize(
    ("member", "wire"),
    [(SessionState.STATELESS, "stateless"), (SessionState.PERSISTENT_AGENT, "persistent-agent")],
)
def test_wire_values_are_stable(member: SessionState, wire: str) -> None:
    # These strings land in operator-facing tables and recorded run data, so a
    # rename is a breaking change and should fail here first.
    assert str(member) == wire
    assert SessionState(wire) is member


def test_unrecognised_value_fails_closed() -> None:
    # Consistent with the other axes: an unknown value is a construction-time
    # error, not a silently-tolerated string.
    with pytest.raises(ValueError, match="not a valid SessionState"):
        SessionState("federated-brain")
