"""Tests for WEB-019: Audit log endpoint with search and filtering."""

from __future__ import annotations

from bernstein.core.routes.audit_log import (
    AuditLogQuery,
    filter_events,
    paginate,
)


class TestAuditLogQuery:
    def test_defaults(self) -> None:
        q = AuditLogQuery()
        assert q.page == 1
        assert q.page_size == 50
        assert q.event_type is None

    def test_offset(self) -> None:
        q = AuditLogQuery(page=3, page_size=20)
        assert q.offset == 40


class TestFilterEvents:
    def test_filter_by_event_type(self) -> None:
        events = [
            {"event_type": "task.transition", "details": {}},
            {"event_type": "agent.spawn", "details": {}},
            {"event_type": "task.transition", "details": {}},
        ]
        result = filter_events(events, event_type="task.transition")
        assert len(result) == 2

    def test_filter_by_search(self) -> None:
        events = [
            {"event_type": "task.transition", "details": {"message": "completed backend task"}},
            {"event_type": "task.transition", "details": {"message": "started frontend task"}},
        ]
        result = filter_events(events, search="backend")
        assert len(result) == 1

    def test_no_filters(self) -> None:
        events = [{"event_type": "a"}, {"event_type": "b"}]
        result = filter_events(events)
        assert len(result) == 2

    def test_filter_by_time_range(self) -> None:
        events = [
            {"event_type": "a", "timestamp": "2026-04-01T00:00:00Z"},
            {"event_type": "b", "timestamp": "2026-04-03T00:00:00Z"},
            {"event_type": "c", "timestamp": "2026-04-05T00:00:00Z"},
        ]
        result = filter_events(
            events,
            from_ts="2026-04-02T00:00:00Z",
            to_ts="2026-04-04T00:00:00Z",
        )
        assert len(result) == 1
        assert result[0]["event_type"] == "b"


class TestPaginate:
    def test_first_page(self) -> None:
        items = list(range(100))
        page = paginate(items, page=1, page_size=10)
        assert page == list(range(10))

    def test_second_page(self) -> None:
        items = list(range(100))
        page = paginate(items, page=2, page_size=10)
        assert page == list(range(10, 20))

    def test_last_page_partial(self) -> None:
        items = list(range(25))
        page = paginate(items, page=3, page_size=10)
        assert page == [20, 21, 22, 23, 24]

    def test_beyond_range(self) -> None:
        items = list(range(5))
        page = paginate(items, page=2, page_size=10)
        assert page == []
