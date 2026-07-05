"""Regression tests for app-factory isolation and the suite memory guard.

Combined pytest batches that included app-creating suites (test_dashboard
among them) used to fail late in the run: every ``create_app`` call appended
a full copy of the /api/v1 route set to a shared module-level router, so
each successive app instance grew by ~220 routes. Route tables and RSS grew
without bound across the session, app startup eventually died with
``RecursionError``, and the suite memory guard then converted the crossing
into a teardown error on every remaining test. Each file passed in
isolation, which made the batch failure look like cross-file pollution.

These tests pin both invariants cheaply so the pollution cannot return:

* building an app must not mutate shared router state, and
* the memory guard must abort the session exactly once, based on current
  (not lifetime-peak) RSS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _pytest.outcomes import Exit

from bernstein.core.routes import api_v1
from bernstein.core.server import create_app
from tests.conftest import _MAX_RSS_BYTES, _enforce_memory_guard

if TYPE_CHECKING:
    from pathlib import Path


def test_create_app_does_not_grow_route_table_across_instances(tmp_path: Path) -> None:
    """Two consecutive apps must have identical route tables.

    Before the fix the second app inherited an extra copy of the whole
    /api/v1 route set from the shared module-level router, so its route
    count was ~220 higher than the first app's.
    """
    app_one = create_app(jsonl_path=tmp_path / "one" / "tasks.jsonl")
    app_two = create_app(jsonl_path=tmp_path / "two" / "tasks.jsonl")

    assert len(app_two.routes) == len(app_one.routes)


def test_create_app_does_not_mutate_shared_api_v1_router(tmp_path: Path) -> None:
    """Building an app must leave the module-level v1 router untouched."""
    routes_before = len(api_v1.router.routes)

    create_app(jsonl_path=tmp_path / "tasks.jsonl")

    assert len(api_v1.router.routes) == routes_before


def test_build_router_returns_fresh_instances() -> None:
    """Each call must return a distinct, empty router."""
    first = api_v1.build_router()
    second = api_v1.build_router()

    assert first is not second
    assert first.routes == []
    assert second.routes == []


def test_memory_guard_below_cap_is_noop() -> None:
    """RSS below the cap must not interrupt the run."""
    _enforce_memory_guard(_MAX_RSS_BYTES - 1)


def test_memory_guard_above_cap_aborts_session_once() -> None:
    """RSS above the cap must stop the session via pytest.exit.

    ``pytest.exit`` raises ``_pytest.outcomes.Exit``, which ends the run
    exactly once. The old ``sys.exit(137)`` raised ``SystemExit`` from a
    fixture teardown, which pytest records as a per-test ERROR and keeps
    running - one crossing used to error every remaining test in the batch.
    """
    with pytest.raises(Exit) as excinfo:
        _enforce_memory_guard(_MAX_RSS_BYTES + 1)

    assert not isinstance(excinfo.value, SystemExit)
    assert excinfo.value.returncode == 137
