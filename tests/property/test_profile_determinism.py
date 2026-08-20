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

`test_deep_profile_still_randomizes` was observed failing in CI (Linux,
`ubuntu-latest`, twice in a row) with `get_profile("deep").derandomize`
reading `True`, and would not reproduce locally across dozens of runs of the
exact same command. Root-caused in #4118: the registration in `conftest.py`
omits a `derandomize` kwarg for `deep`, which looks like it defers to
hypothesis's own built-in default (`False`) -- but `settings.register_profile`'s
parent-inheritance falls back to `settings.default`, and hypothesis itself
loads a CI-specific profile as that default whenever it detects a CI
environment (`CI=true` and friends), *before* `conftest.py` runs, with
`derandomize=True`. Off CI, `settings.default` is hypothesis's neutral
built-in (`derandomize=False`), which is exactly why this only ever
surfaced in CI and never locally: `CI=true GITHUB_ACTIONS=true python -c
"from hypothesis import settings; print(settings.default.derandomize)"`
prints `True`; the same command with no CI env prints `False`.
`conftest.py` now passes `derandomize=False` explicitly on the `deep`
registration, closing the inheritance path regardless of environment.

Every value these tests check is still captured once at collection time
(module import), not re-queried via `get_profile(...)` from inside a test
body -- see the module-level constants below. This is now redundant with
the `conftest.py` fix for `test_deep_profile_still_randomizes` specifically,
but it costs nothing and keeps all three tests in this file resolving their
inputs the same way (matching how every other property-test file's own
`@settings(...)` decorators already resolve at definition/collection time,
not from inside the running test).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

_CONFTEST_PATH = Path(__file__).parent / "conftest.py"

# Captured at collection time (module import), not re-queried inside the
# test bodies below. `hypothesis.settings.get_profile(name)` reads a live
# registry entry that is, in principle, re-writable for the remainder of
# the process by any later `register_profile(name, ...)` call anywhere in
# the session -- this repo has none today (grepped tests/ and src/), but a
# value captured here is immune to one appearing later regardless, and to
# whatever the registry does under a specific runner's timing. Collection
# happens before any test's body executes, so nothing collected after this
# module can have run yet.
_SMOKE_PROFILE_AT_COLLECTION = hypothesis_settings.get_profile("smoke")
_DEEP_DERANDOMIZE_AT_COLLECTION = hypothesis_settings.get_profile("deep").derandomize


def test_smoke_profile_is_derandomized() -> None:
    """The registered profile the required CI job actually loads is pinned."""
    assert _SMOKE_PROFILE_AT_COLLECTION.derandomize is True


def test_deep_profile_still_randomizes() -> None:
    """Guards the #4024 trap-A failure mode: a fix that freezes deep too.

    `deep` exists to explore fresh input space on the nightly cadence; a
    new draw finding a defect there is correct behaviour, not a flake.
    """
    assert _DEEP_DERANDOMIZE_AT_COLLECTION is False


def _generated_values_under_smoke() -> list[int]:
    """Run one small derandomized property sweep and return every draw.

    A self-contained ``@given`` test rather than reusing one of the 33
    existing property-test files: this keeps the determinism check from
    drifting if any of those files' own strategies change shape, and it
    exercises the exact registered ``smoke`` profile object -- not a
    hand-copied approximation of its kwargs -- via the object captured at
    collection time (``_SMOKE_PROFILE_AT_COLLECTION``), not a fresh
    ``get_profile("smoke")`` call made from inside a test body.
    """
    collected: list[int] = []

    @given(value=st.integers())
    @hypothesis_settings(_SMOKE_PROFILE_AT_COLLECTION)
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


def _deep_derandomize_in_subprocess(*, ci: bool) -> bool:
    """Import ``conftest.py`` fresh in a subprocess; report ``deep``'s resolved value.

    A subprocess, not a monkeypatched ``CI`` env var in this process:
    hypothesis resolves its CI-specific default profile the first time
    ``hypothesis`` itself is imported in a process, so flipping the env var
    afterward and re-importing ``conftest`` would not re-resolve it -- this
    process already imported hypothesis once, driving these very tests.
    A fresh interpreter run under a controlled environment is the only way
    to observe what a real CI runner, or a real developer laptop, actually
    sees.
    """
    env = os.environ.copy()
    if ci:
        env["CI"] = "true"
        env["GITHUB_ACTIONS"] = "true"
    else:
        env.pop("CI", None)
        env.pop("GITHUB_ACTIONS", None)
    script = (
        "import runpy; "
        f"runpy.run_path({str(_CONFTEST_PATH)!r}, run_name='conftest'); "
        "from hypothesis import settings; "
        "print(settings.get_profile('deep').derandomize)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip() == "True"


def test_deep_derandomize_is_false_under_both_ci_and_no_ci() -> None:
    """#4118's own proof: pin the resolved value under both environments, not just the ambient one.

    The defect #4118 found was invisible precisely because the environment
    running this suite locally is the one where it looks fine:
    ``derandomize=False`` on a developer laptop was correct while CI
    silently read ``True`` by inheritance from hypothesis's own
    CI-detection default -- the two environments disagreed, and nobody
    inspects the registered value interactively in CI. A parametrize over a
    monkeypatched ``CI`` variable would not catch this (see
    ``_deep_derandomize_in_subprocess``'s docstring for why); a subprocess
    per environment is what #4118 asked for and what would have caught the
    original defect before it shipped.
    """
    assert _deep_derandomize_in_subprocess(ci=True) is False
    assert _deep_derandomize_in_subprocess(ci=False) is False
