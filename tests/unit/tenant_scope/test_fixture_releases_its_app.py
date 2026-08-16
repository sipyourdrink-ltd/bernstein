"""The app a case builds must not outlive that case.

``fx`` builds a whole ``create_app`` stack per test, deliberately: the suites
next door prove that one tenant's rows never reach another tenant's view, and
a fixture shared between cases would let state from one reach the next.  The
cost of that choice is that the heap grows with the number of cases unless
each app is released when its case ends, and #3927 is what happens when it is
not - every subsequent gc pass walks every app ever built, so per-case
teardown climbs and the file's total cost is quadratic in its own length until
it crosses the per-file timeout in ``scripts/run_tests.py``.

The property is asserted here rather than eyeballed from a probe, because a
regression in it is silent: nothing fails, the suite just gets slower until it
does not finish.  The measurement is the slope, not any single reading - the
absolute object count depends on what else the session has imported, and the
app under the current case is alive on purpose in every sample, so it cancels.
"""

from __future__ import annotations

import gc
from typing import TYPE_CHECKING, Any

import pytest

# ``anyio_backend``, ``client``, ``fx``, ``jsonl_path`` and ``sdd_dir`` are
# fixtures: pytest resolves them from this module's namespace, so importing
# them here is what makes them available to the tests below.
from tests.unit.tenant_scope.conftest import (
    anyio_backend,  # noqa: F401
    client,  # noqa: F401
    fx,  # noqa: F401
    jsonl_path,  # noqa: F401
    sdd_dir,  # noqa: F401
)

if TYPE_CHECKING:
    from httpx import AsyncClient

    from tests.unit.tenant_scope.conftest import Fixture

# ruff: noqa: F811

pytestmark = [pytest.mark.ci, pytest.mark.auth_enabled]

# The route each sample calls, and it is the whole reason this file is a
# useful gate rather than a decoration.  Measured on this tree at 0d4e7db,
# retention follows how the handler is declared, not what it reads:
#
#     /observability/deps    def        1 app retained per case
#     /observability/agents  def        1 app retained per case
#     /team                  def        1 app retained per case
#     /recap                 async def  0
#     /dashboard/auth/status async def  0
#
# So a sampler pointed at an async route sees a flat heap on a tree that
# retains every app, and passes while proving nothing.  ``/observability/deps``
# is a plain read with no seeding, and it is one of the routes the suite this
# file guards actually exercises.
SAMPLED_ROUTE = "/observability/deps"

# Enough cases to read a slope from, few enough to stay cheap: each one builds
# a full app, which is the expensive thing this file is about.
SAMPLES = 5

# One ``create_app`` costs ~13,100 tracked objects on this tree, measured by
# building three and dropping them; held through a request it is ~19,700.  A
# released app leaves ~120 behind, the same figure as never holding it at all.
# The budget sits between the two and far enough from both that it is neither
# flaky nor vacuous: a tree that retains its apps overshoots it by ~10x, and
# one that releases them comes in two orders of magnitude under.
BUDGET_OBJECTS_PER_APP = 2_000

# Filled by the sampling cases below and read by the assertions at the end.
_live: list[int] = []
_task_ids: list[str] = []


@pytest.mark.anyio()
@pytest.mark.parametrize("sample", range(SAMPLES))
async def test_each_case_gets_a_working_app_of_its_own(
    fx: Fixture,
    client: AsyncClient,
    sample: int,
) -> None:
    """Build an app, serve a request from it, and record the heap.

    The request matters: an app that was built and never used holds none of
    the per-request state that makes the retention expensive, so a sampler
    that skipped it would measure less than the suite it stands in for.
    """
    response = await client.get(SAMPLED_ROUTE, headers=fx.credentials["legacy_bearer"].headers)

    assert response.status_code == 200, f"{SAMPLED_ROUTE} answered {response.status_code}"
    gc.collect()
    _live.append(len(gc.get_objects()))
    _task_ids.append(fx.task_a_id)


def test_the_heap_does_not_grow_with_the_number_of_apps_built() -> None:
    """The apps built by earlier cases are gone by the time later ones run."""
    if len(_live) < SAMPLES:
        pytest.skip(f"needs all {SAMPLES} sampling cases in this module to have run")

    # The first sample carries the one-time imports the first request through
    # the stack performs, which is a fixed cost rather than a per-app one.
    warm = _live[1:]
    per_app = (warm[-1] - warm[0]) / (len(warm) - 1)

    assert per_app < BUDGET_OBJECTS_PER_APP, (
        f"the heap grew by {per_app:.0f} tracked objects per app across "
        f"{len(warm)} samples {warm}, which is the shape of an app that "
        f"outlives its case; see conftest._release"
    )


def test_every_case_really_did_build_its_own_app() -> None:
    """The control on the test above: it must not pass by sharing one app.

    Object identity cannot answer this - a released app's ``id`` is free to be
    handed to the next one - so this reads the seeded tasks, which are created
    fresh per app and carry ids that are unique for the life of the session.
    """
    if len(_task_ids) < SAMPLES:
        pytest.skip(f"needs all {SAMPLES} sampling cases in this module to have run")

    assert len(set(_task_ids)) == SAMPLES, f"cases shared an app: {_task_ids}"


@pytest.mark.anyio()
async def test_the_app_is_intact_while_its_own_case_runs(fx: Fixture, client: AsyncClient) -> None:
    """The release must land after the case, not during it.

    Without this, a ``_release`` that ran too early would satisfy every
    assertion above while breaking every other suite that shares this
    conftest, and the failure would surface somewhere else as an unrelated
    404.
    """
    app: Any = fx.app

    assert app.router.routes, "the app under test has no routes"
    assert app.state.store is not None
    response = await client.get(SAMPLED_ROUTE, headers=fx.credentials["legacy_bearer"].headers)
    assert response.status_code == 200
