"""Determinism of the smoke-profile Schemathesis sweep (#4024).

`test_task_server_schemathesis.py`'s ``test_no_unhandled_exceptions`` gates
both the PR lane and every merge-queue batch. Before #4024's fix its
``@settings(...)`` decorator set no ``derandomize`` kwarg at all -- which,
per #4118, does not mean "randomized" the way it looks: an unset kwarg is
inherited from ``settings.default``, and hypothesis's own library loads a
CI-specific profile as that default whenever it detects a CI environment
(``derandomize=True``, before any of this repository's code runs). So in
CI specifically -- the one place this lane actually gates anything -- the
pre-fix behaviour was already derandomized by that inheritance, same as
``tests/property/conftest.py``'s ``deep`` profile (#4118's other case).
What #4024's fix actually buys is not "CI now reproduces where it didn't":
it is a value that no longer depends on hypothesis's CI-detection heuristic
or on running under CI at all -- a laptop run and a CI run now agree,
which they did not before, and the result survives hypothesis changing how
it detects CI in a future release.

The fix branches ``derandomize`` on ``_PROFILE`` so only the *gating* smoke
lane is pinned; the nightly deep lane keeps exploring fresh input space,
which is where new defects should be found. These tests assert the property
directly rather than trusting the settings object was wired correctly by
inspection alone -- one for each half of the branch, plus the reproducibility
property itself, driven by the settings the module under test actually
applies (not a fresh literal) so a deleted fix fails the test that claims to
cover it rather than passing for an unrelated, upstream reason.

Deliberately a sibling file, not additions to ``test_task_server_schemathesis.py``:
that file's own docstring scopes it to "fuzz documented operations ... assert
no 500-class crash leaks". These assert properties of the ``@settings`` object
and of the generated-case set, not app behaviour, and bundling them in would
blur that file's stated purpose.
"""

from __future__ import annotations

import re

import pytest
import schemathesis
from hypothesis import given, settings

# Imported as a module, not `from ... import test_no_unhandled_exceptions`:
# schemathesis's own pytest plugin collects any object carrying its schema
# mark by walking a module's *own* top-level attributes
# (schemathesis.core.marks.Mark.get reads an attribute set directly on the
# function object - there is no name-prefix check pytest's default
# collector applies first). Binding the target function itself as a
# top-level name here would make schemathesis re-collect and re-run the
# entire smoke sweep a second time, under this file, on every run.
# Referencing it through the module keeps it off this file's own
# top-level namespace, so only its home file collects it.
from tests.contract import test_task_server_schemathesis as _schemathesis_module

_MAX_EXAMPLES = _schemathesis_module._MAX_EXAMPLES
_PROFILE = _schemathesis_module._PROFILE
_SMOKE_PATH_PREFIXES = _schemathesis_module._SMOKE_PATH_PREFIXES
schema = _schemathesis_module.schema


def test_smoke_profile_sets_derandomize() -> None:
    """Under the profile this session actually resolved, smoke pins its draws.

    ``_PROFILE`` (and therefore ``_DERANDOMIZE_SMOKE``) is read from the
    environment at import time in the module under test, so this asserts
    against whichever profile the *current* process resolved to rather than
    setting the env var itself -- reimporting the already-imported module
    under a different env var would not change anything it already computed
    at import time. CI's ``schemathesis-smoke`` job is what actually needs
    this to hold; see ``test_deep_profile_still_randomizes`` below for the
    other half of the branch.
    """
    if _PROFILE != "smoke":
        pytest.skip(f"this process resolved SCHEMATHESIS_PROFILE={_PROFILE!r}, not 'smoke'")
    applied = _schemathesis_module.test_no_unhandled_exceptions._hypothesis_internal_use_settings
    assert applied.derandomize is True


def test_deep_profile_still_randomizes() -> None:
    """The nightly deep lane must keep exploring; #4024 forbids freezing it too.

    Written first, per the issue brief: a fix that only checks the smoke
    side can ship green while accidentally setting ``derandomize=True``
    unconditionally, which would freeze the deep lane's exploration and
    silently violate the acceptance criterion that forbids it.
    """
    if _PROFILE != "deep":
        pytest.skip(f"this process resolved SCHEMATHESIS_PROFILE={_PROFILE!r}, not 'deep'")
    applied = _schemathesis_module.test_no_unhandled_exceptions._hypothesis_internal_use_settings
    assert applied.derandomize is False


def _smoke_case_strategy():
    """The same operation subset the smoke lane fuzzes, as a bare Hypothesis strategy.

    ``schema.parametrize()`` drives Hypothesis through pytest's own
    collection machinery, which runs every example inside one pytest
    invocation rather than exposing them as separately inspectable results.
    Driving the strategy directly with ``@given`` -- schemathesis's own
    documented "use with @given in non-Schemathesis tests" escape hatch --
    lets this test capture and compare the exact generated cases across two
    independent runs, which is the property #4024 asks for.
    """
    prefix_pattern = "|".join(re.escape(prefix) for prefix in _SMOKE_PATH_PREFIXES)
    return schema.include(path_regex=f"^({prefix_pattern})").as_strategy()


def _generated_cases() -> list[tuple[str, str, str, str, str]]:
    """Run one smoke-sized derandomized sweep and return each case's identity.

    Method, path, and every generated part are captured -- not just
    method+path -- so two runs that hit the same operations with different
    generated bodies still count as different, matching what the acceptance
    criterion means by "the same set of generated cases".
    """
    collected: list[tuple[str, str, str, str, str]] = []
    strategy = _smoke_case_strategy()
    applied = _schemathesis_module.test_no_unhandled_exceptions._hypothesis_internal_use_settings

    @given(case=strategy)
    @settings(applied)
    def _run(case: schemathesis.Case) -> None:
        collected.append(
            (
                case.method,
                case.path,
                repr(case.path_parameters),
                repr(case.query),
                repr(case.body),
            )
        )

    _run()
    return collected


def test_two_consecutive_smoke_runs_generate_identical_cases() -> None:
    """The property #4024's acceptance criteria name directly.

    Two independent sweeps, driven by the settings the smoke lane actually
    applies (not a fresh hardcoded ``derandomize=True`` literal, which would
    only prove Hypothesis's own replay contract and pass even if
    ``_DERANDOMIZE_SMOKE`` were deleted -- caught in review on #4105): the
    generated case sequence must match exactly, not just up to reordering,
    which is what "the same set of generated cases" means for a lane that
    is supposed to give the same verdict on the same commit every time.

    Skipped outside the smoke profile for the same reason
    ``test_smoke_profile_sets_derandomize`` is: under ``deep``,
    ``applied.derandomize`` is ``False`` by design (deep is the lane meant
    to explore fresh input space), so two independent sweeps have no reason
    to agree there -- asserting equality unconditionally would make this
    test flake in the nightly deep job for behaving exactly as intended.
    """
    if _PROFILE != "smoke":
        pytest.skip(f"this process resolved SCHEMATHESIS_PROFILE={_PROFILE!r}, not 'smoke'")
    first = _generated_cases()
    second = _generated_cases()
    assert first, "the smoke path allow-list matched no operations; nothing was exercised"
    assert first == second
