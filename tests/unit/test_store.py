"""Tests for the BaseTaskStore abstract base class and its dataclasses."""

from __future__ import annotations

import time

import pytest

from bernstein.core.persistence.store import BaseTaskStore, RoleSummary, StatusSummary

# --- RoleSummary tests ---


class TestRoleSummary:
    """Tests for the RoleSummary dataclass."""

    def test_default_values(self) -> None:
        rs = RoleSummary(role="backend", open=5, claimed=3, done=10, failed=1)
        assert rs.role == "backend"
        assert rs.open == 5
        assert rs.claimed == 3
        assert rs.done == 10
        assert rs.failed == 1
        assert rs.cost_usd == pytest.approx(0.0)

    def test_custom_cost(self) -> None:
        rs = RoleSummary(role="frontend", open=2, claimed=1, done=4, failed=0, cost_usd=1.25)
        assert rs.cost_usd == pytest.approx(1.25)

    def test_zero_counts(self) -> None:
        rs = RoleSummary(role="qa", open=0, claimed=0, done=0, failed=0)
        assert rs.open == 0
        assert rs.done == 0


# --- StatusSummary tests ---


class TestStatusSummary:
    """Tests for the StatusSummary dataclass."""

    def test_default_values(self) -> None:
        ss = StatusSummary(total=20, open=5, claimed=3, done=10, failed=2)
        assert ss.total == 20
        assert ss.per_role == []
        assert ss.total_cost_usd == pytest.approx(0.0)

    def test_with_per_role(self) -> None:
        roles = [
            RoleSummary(role="backend", open=3, claimed=2, done=5, failed=1),
            RoleSummary(role="frontend", open=2, claimed=1, done=5, failed=1),
        ]
        ss = StatusSummary(total=20, open=5, claimed=3, done=10, failed=2, per_role=roles)
        assert len(ss.per_role) == 2
        assert ss.per_role[0].role == "backend"

    def test_with_custom_cost(self) -> None:
        ss = StatusSummary(total=10, open=2, claimed=1, done=6, failed=1, total_cost_usd=5.50)
        assert ss.total_cost_usd == pytest.approx(5.50)


# --- BaseTaskStore tests ---


class TestBaseTaskStore:
    """Tests for the BaseTaskStore abstract class."""

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            BaseTaskStore()  # type: ignore[abstract]

    def test_start_ts_set_on_init(self) -> None:
        """Concrete subclass should have _start_ts set at creation."""

        class ConcreteStore(BaseTaskStore):
            async def startup(self) -> None: ...
            async def shutdown(self) -> None: ...
            async def create(self, req):  # type: ignore[override]
                ...
            async def claim_next(self, role):  # type: ignore[override]
                ...
            async def claim_by_id(self, task_id, expected_version=None, agent_role=None):  # type: ignore[override]
                ...
            async def claim_batch(self, task_ids, agent_id, agent_role=None):  # type: ignore[override]
                ...
            async def complete(self, task_id, result_summary):  # type: ignore[override]
                ...
            async def fail(self, task_id, reason):  # type: ignore[override]
                ...
            async def add_progress(self, task_id, message, percent):  # type: ignore[override]
                ...
            async def update(self, task_id, role, priority):  # type: ignore[override]
                ...
            async def cancel(self, task_id, reason):  # type: ignore[override]
                ...
            async def list_tasks(self, status=None, cell_id=None):  # type: ignore[override]
                ...
            async def get_task(self, task_id):  # type: ignore[override]
                ...
            async def status_summary(self):  # type: ignore[override]
                ...
            async def read_archive(self, limit=50):  # type: ignore[override]
                ...
            async def heartbeat(self, agent_id, role, status):  # type: ignore[override]
                ...
            async def mark_stale_dead(self, threshold_s=60.0):  # type: ignore[override]
                ...

            @property
            def agent_count(self) -> int:
                return 0

        before = time.time()
        store = ConcreteStore()
        after = time.time()
        assert before <= store.start_ts <= after

    def test_subclass_must_implement_abstracts(self) -> None:
        """An incomplete subclass cannot be instantiated."""

        class IncompleteStore(BaseTaskStore):
            async def startup(self) -> None: ...
            async def shutdown(self) -> None: ...

            # Missing all other abstracts

        with pytest.raises(TypeError):
            IncompleteStore()  # type: ignore[abstract]

    def test_init_subclass_hook(self) -> None:
        """__init_subclass__ is callable without error."""

        class AnotherStore(BaseTaskStore):
            async def startup(self) -> None: ...
            async def shutdown(self) -> None: ...
            async def create(self, req):  # type: ignore[override]
                ...
            async def claim_next(self, role):  # type: ignore[override]
                ...
            async def claim_by_id(self, task_id, expected_version=None, agent_role=None):  # type: ignore[override]
                ...
            async def claim_batch(self, task_ids, agent_id, agent_role=None):  # type: ignore[override]
                ...
            async def complete(self, task_id, result_summary):  # type: ignore[override]
                ...
            async def fail(self, task_id, reason):  # type: ignore[override]
                ...
            async def add_progress(self, task_id, message, percent):  # type: ignore[override]
                ...
            async def update(self, task_id, role, priority):  # type: ignore[override]
                ...
            async def cancel(self, task_id, reason):  # type: ignore[override]
                ...
            async def list_tasks(self, status=None, cell_id=None):  # type: ignore[override]
                ...
            async def get_task(self, task_id):  # type: ignore[override]
                ...
            async def status_summary(self):  # type: ignore[override]
                ...
            async def read_archive(self, limit=50):  # type: ignore[override]
                ...
            async def heartbeat(self, agent_id, role, status):  # type: ignore[override]
                ...
            async def mark_stale_dead(self, threshold_s=60.0):  # type: ignore[override]
                ...

            @property
            def agent_count(self) -> int:
                return 42

        store = AnotherStore()
        assert store.agent_count == 42
