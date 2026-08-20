"""Shared Hypothesis configuration for the property-test suite.

PR-time runs the ``smoke`` profile (50 examples, 5 s deadline) so each
file completes in under ~30 s on a GitHub-hosted runner. The nightly
``deep`` profile lifts the example budget to 1 000 and removes the
deadline so rare counter-examples still surface.

Profile selection follows ``HYPOTHESIS_PROFILE`` (the variable name
hypothesis itself reads via ``settings.load_profile``); if unset the
``smoke`` profile is used. CI workflows export this explicitly so
behaviour is unambiguous regardless of caller.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, Verbosity, settings

# ``smoke`` - PR-time. Tight budget; flake-resistant.
#
# derandomize=True (#4044, the same defect class as #4024 for the
# schemathesis lane): PR-time and merge-queue gates must give the same
# verdict for the same commit, or a pathological draw dequeues an
# otherwise-clean merge-queue batch for a defect that was not actually
# new. Not on "deep" - that profile exists to explore fresh input space,
# and freezing it would remove its value. Hypothesis derives the
# derandomized seed from the test function itself, so there is no seed
# value to log or maintain here.
#
# What "the same example set" is NOT pinned to: Hypothesis itself.
# derandomize replays *this resolved Hypothesis version's* deterministic
# sequence, so a routine `uv lock --upgrade` that bumps hypothesis can
# shift which cases the smoke lane draws even with this fix in place.
# That is expected, not a regression.
settings.register_profile(
    "smoke",
    max_examples=50,
    deadline=5_000,  # 5 s per example - generous for property cases that
    # touch the filesystem (WAL writer, audit log).
    derandomize=True,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
        HealthCheck.function_scoped_fixture,
    ],
    verbosity=Verbosity.normal,
)

# ``deep`` - nightly. Thoroughness over speed.
#
# derandomize=False is explicit, not the implicit default it looks like
# (#4118). register_profile() inherits any unset kwarg from
# `settings.default`, and hypothesis itself loads a CI-specific profile as
# that default whenever it detects a CI environment (`CI=true` and
# friends) -- *before* this module runs, with database=None and
# derandomize=True. Off CI, `settings.default` is hypothesis's neutral
# built-in (derandomize=False), which is why this only ever surfaced in
# CI and never locally. Pinning the value here removes the dependency on
# which environment happens to be running this file.
settings.register_profile(
    "deep",
    max_examples=1_000,
    deadline=None,
    derandomize=False,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
        HealthCheck.function_scoped_fixture,
    ],
    verbosity=Verbosity.verbose,
    print_blob=True,
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "smoke"))
