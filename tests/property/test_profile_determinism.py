"""Determinism of the registered `smoke` Hypothesis profile (#4044).

Every property-test file under `tests/property/` inherits its `@settings`
from the two profiles `conftest.py` registers, selected via
`HYPOTHESIS_PROFILE`. Before #4044's fix, neither profile set `derandomize`
or a seed, so the *required* `property-tests` CI job -- which gates both
the PR lane and every merge-queue batch, exactly like the schemathesis lane
#4024 fixed -- drew a fresh example set on every run. The same tree could
pass on one run and fail on the next.

The fix sets `derandomize=True` on the registered `smoke` profile only,
leaving `deep` (nightly, meant to explore fresh input space) untouched.
These tests assert that property directly rather than trusting the
registration was correct by inspection alone.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st


def test_smoke_profile_is_derandomized() -> None:
    """The registered profile the required CI job actually loads is pinned."""
    assert hypothesis_settings.get_profile("smoke").derandomize is True


def test_deep_profile_still_randomizes() -> None:
    """Guards the #4024 trap-A failure mode: a fix that freezes deep too.

    `deep` exists to explore fresh input space on the nightly cadence; a
    new draw finding a defect there is correct behaviour, not a flake.
    """
    assert hypothesis_settings.get_profile("deep").derandomize is False


def _generated_values_under_smoke() -> list[int]:
    """Run one small derandomized property sweep and return every draw.

    A self-contained ``@given`` test rather than reusing one of the 33
    existing property-test files: this keeps the determinism check from
    drifting if any of those files' own strategies change shape, and it
    exercises the exact registered ``smoke`` profile object -- not a
    hand-copied approximation of its kwargs -- via
    ``hypothesis_settings.get_profile("smoke")``.
    """
    collected: list[int] = []

    @given(value=st.integers())
    @hypothesis_settings(hypothesis_settings.get_profile("smoke"))
    def _run(value: int) -> None:
        collected.append(value)

    _run()
    return collected


def test_two_consecutive_smoke_runs_are_identical() -> None:
    """The property #4044's acceptance criteria name directly.

    Two independent derandomized sweeps under the same settings, same
    process: the generated value sequence must match exactly, not just up
    to reordering -- Hypothesis's own derandomize contract is that replay
    is deterministic *and* ordered.
    """
    first = _generated_values_under_smoke()
    second = _generated_values_under_smoke()
    assert first, "the smoke profile generated no examples; nothing was exercised"
    assert first == second
