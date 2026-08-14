"""Tests for issue #1728 finding 2: shared ``_tasks_by_id`` helper.

The two observability view helpers (``/observability/agents`` and
``/observability/token-breakdown``) previously each rebuilt
``{task.id: task for task in store.list_tasks()}``. On a 10k-task store
that doubled the dict-construction cost. The shared helper caches the
materialised dict on ``request.state`` so any subsequent caller inside the
same request reuses it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bernstein.core.routes.observability import _tasks_by_id

TENANT = "tenant-under-test"


class _FakeStore:
    """Minimal stub matching the ``TaskStore.list_tasks`` surface used here.

    ``list_tasks`` narrows on ``tenant_id`` the way the real store does, so a
    helper that stopped passing the scope through shows up here as rows it
    should not have materialised rather than as a silently ignored argument.
    """

    def __init__(self, tasks: list[Any]) -> None:
        self._tasks = tasks
        self.calls = 0

    def list_tasks(self, tenant_id: str | None = None) -> list[Any]:
        self.calls += 1
        if tenant_id is None:
            return self._tasks
        return [t for t in self._tasks if getattr(t, "tenant_id", TENANT) == tenant_id]


def _fake_request() -> Any:
    """A request stub with a mutable ``state`` namespace."""
    return SimpleNamespace(state=SimpleNamespace())


def test_tasks_by_id_returns_id_keyed_dict() -> None:
    """Building the dict matches the previous comprehension's shape."""
    tasks = [SimpleNamespace(id="a"), SimpleNamespace(id="b")]
    store = _FakeStore(tasks)
    result = _tasks_by_id(_fake_request(), store, TENANT)

    assert set(result.keys()) == {"a", "b"}
    assert result["a"] is tasks[0]
    assert result["b"] is tasks[1]


def test_tasks_by_id_caches_on_request_state() -> None:
    """The helper materialises once per request and reuses the cached dict."""
    tasks = [SimpleNamespace(id="a")]
    store = _FakeStore(tasks)
    request = _fake_request()

    first = _tasks_by_id(request, store, TENANT)
    second = _tasks_by_id(request, store, TENANT)

    assert first is second
    assert store.calls == 1, "list_tasks should only be invoked once per request"


def test_tasks_by_id_rebuilds_for_new_request() -> None:
    """Cache is request-scoped: a new request rebuilds the dict."""
    tasks = [SimpleNamespace(id="a")]
    store = _FakeStore(tasks)

    _tasks_by_id(_fake_request(), store, TENANT)
    _tasks_by_id(_fake_request(), store, TENANT)

    assert store.calls == 2, "fresh request must trigger a fresh materialisation"


def test_tasks_by_id_materialises_only_the_named_tenant() -> None:
    """The scope reaches the store rather than being accepted and dropped.

    ``tenant_id`` is a required argument, so a regression here is not a
    forgotten call site but a helper that takes the scope and then reads the
    table without it.
    """
    mine = SimpleNamespace(id="mine", tenant_id=TENANT)
    theirs = SimpleNamespace(id="theirs", tenant_id="another-tenant")
    store = _FakeStore([mine, theirs])

    result = _tasks_by_id(_fake_request(), store, TENANT)

    assert set(result.keys()) == {"mine"}
