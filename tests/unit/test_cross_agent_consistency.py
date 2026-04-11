"""Tests for the cross-agent consistency checker.

Covers schema compatibility, method consistency, error-code alignment,
and the overall check_consistency() orchestration logic.
"""

from __future__ import annotations

from bernstein.core.cross_agent_consistency import (
    AgentImplementation,
    ApiContract,
    ConsistencyIssueType,
    ConsistencyReport,
    check_consistency,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_BACKEND_REQ = {"name": "str", "quantity": "int"}
_BACKEND_RES = {"id": "str", "name": "str"}
_FRONTEND_REQ = {"name": "str", "quantity": "int"}
_FRONTEND_RES = {"id": "str"}


def _backend(
    endpoint: str = "/api/items",
    method: str = "POST",
    request_fields: dict[str, str] | None = None,
    response_fields: dict[str, str] | None = None,
    error_codes: list[int] | None = None,
    agent_id: str = "backend-001",
) -> AgentImplementation:
    return AgentImplementation(
        agent_id=agent_id,
        role="backend",
        contracts=[
            ApiContract(
                endpoint=endpoint,
                method=method,
                request_fields=_BACKEND_REQ if request_fields is None else request_fields,
                response_fields=_BACKEND_RES if response_fields is None else response_fields,
                error_codes=[400, 422, 500] if error_codes is None else error_codes,
            )
        ],
    )


def _frontend(
    endpoint: str = "/api/items",
    method: str = "POST",
    request_fields: dict[str, str] | None = None,
    response_fields: dict[str, str] | None = None,
    error_codes: list[int] | None = None,
    agent_id: str = "frontend-001",
) -> AgentImplementation:
    return AgentImplementation(
        agent_id=agent_id,
        role="frontend",
        contracts=[
            ApiContract(
                endpoint=endpoint,
                method=method,
                request_fields=_FRONTEND_REQ if request_fields is None else request_fields,
                response_fields=_FRONTEND_RES if response_fields is None else response_fields,
                error_codes=[400, 422] if error_codes is None else error_codes,
            )
        ],
    )


# ---------------------------------------------------------------------------
# check_consistency — basic wiring
# ---------------------------------------------------------------------------


class TestCheckConsistencyBasic:
    def test_consistent_pair_passes(self) -> None:
        report = check_consistency([_backend(), _frontend()])
        assert report.is_consistent
        assert report.checked_endpoints == 1

    def test_single_impl_returns_clean_report(self) -> None:
        report = check_consistency([_backend()])
        assert report.is_consistent
        assert report.checked_endpoints == 0

    def test_empty_list_returns_clean_report(self) -> None:
        report = check_consistency([])
        assert report.is_consistent
        assert report.checked_endpoints == 0

    def test_two_endpoints_no_overlap_passes(self) -> None:
        b1 = AgentImplementation(
            agent_id="b1",
            role="backend",
            contracts=[ApiContract(endpoint="/api/orders", method="GET")],
        )
        f1 = AgentImplementation(
            agent_id="f1",
            role="frontend",
            contracts=[ApiContract(endpoint="/api/users", method="GET")],
        )
        report = check_consistency([b1, f1])
        assert report.is_consistent
        assert report.checked_endpoints == 0  # no shared endpoints

    def test_returns_consistency_report_type(self) -> None:
        report = check_consistency([_backend(), _frontend()])
        assert isinstance(report, ConsistencyReport)


# ---------------------------------------------------------------------------
# Method mismatch
# ---------------------------------------------------------------------------


class TestMethodMismatch:
    def test_different_methods_raises_issue(self) -> None:
        report = check_consistency(
            [
                _backend(method="POST"),
                _frontend(method="PUT"),
            ]
        )
        assert not report.is_consistent
        issues = report.issues_by_type(ConsistencyIssueType.METHOD_MISMATCH)
        assert len(issues) == 1

    def test_method_mismatch_lists_both_agents(self) -> None:
        report = check_consistency(
            [
                _backend(method="POST", agent_id="b1"),
                _frontend(method="PATCH", agent_id="f1"),
            ]
        )
        issue = report.issues_by_type(ConsistencyIssueType.METHOD_MISMATCH)[0]
        assert "b1" in issue.agents_involved
        assert "f1" in issue.agents_involved

    def test_same_method_no_mismatch(self) -> None:
        report = check_consistency(
            [
                _backend(method="GET"),
                _frontend(method="GET"),
            ]
        )
        assert report.issues_by_type(ConsistencyIssueType.METHOD_MISMATCH) == []

    def test_method_normalised_to_uppercase(self) -> None:
        b = AgentImplementation(
            agent_id="b",
            role="backend",
            contracts=[ApiContract(endpoint="/x", method="post", response_fields={"id": "str"})],
        )
        f = AgentImplementation(
            agent_id="f",
            role="frontend",
            contracts=[ApiContract(endpoint="/x", method="POST", response_fields={"id": "str"})],
        )
        report = check_consistency([b, f])
        assert report.issues_by_type(ConsistencyIssueType.METHOD_MISMATCH) == []


# ---------------------------------------------------------------------------
# Missing request field
# ---------------------------------------------------------------------------


class TestMissingRequestField:
    def test_consumer_sends_field_not_in_producer(self) -> None:
        report = check_consistency(
            [
                _backend(request_fields={"name": "str"}),
                _frontend(request_fields={"name": "str", "extra_field": "int"}),
            ]
        )
        issues = report.issues_by_type(ConsistencyIssueType.MISSING_REQUEST_FIELD)
        assert len(issues) == 1
        assert "extra_field" in issues[0].description

    def test_consumer_subset_of_producer_passes(self) -> None:
        report = check_consistency(
            [
                _backend(request_fields={"name": "str", "quantity": "int", "note": "str"}),
                _frontend(request_fields={"name": "str"}),
            ]
        )
        assert report.issues_by_type(ConsistencyIssueType.MISSING_REQUEST_FIELD) == []

    def test_empty_consumer_request_passes(self) -> None:
        report = check_consistency(
            [
                _backend(request_fields={"name": "str"}),
                _frontend(request_fields={}),
            ]
        )
        assert report.is_consistent


# ---------------------------------------------------------------------------
# Missing response field
# ---------------------------------------------------------------------------


class TestMissingResponseField:
    def test_consumer_reads_field_not_in_producer(self) -> None:
        report = check_consistency(
            [
                _backend(response_fields={"id": "str"}),
                _frontend(response_fields={"id": "str", "created_at": "float"}),
            ]
        )
        issues = report.issues_by_type(ConsistencyIssueType.MISSING_RESPONSE_FIELD)
        assert len(issues) == 1
        assert "created_at" in issues[0].description

    def test_producer_superset_of_consumer_passes(self) -> None:
        report = check_consistency(
            [
                _backend(response_fields={"id": "str", "name": "str", "created_at": "float"}),
                _frontend(response_fields={"id": "str"}),
            ]
        )
        assert report.issues_by_type(ConsistencyIssueType.MISSING_RESPONSE_FIELD) == []


# ---------------------------------------------------------------------------
# Field type mismatch
# ---------------------------------------------------------------------------


class TestFieldTypeMismatch:
    def test_request_field_type_conflict(self) -> None:
        report = check_consistency(
            [
                _backend(request_fields={"quantity": "int"}),
                _frontend(request_fields={"quantity": "str"}),
            ]
        )
        issues = report.issues_by_type(ConsistencyIssueType.FIELD_TYPE_MISMATCH)
        assert len(issues) == 1
        assert "quantity" in issues[0].description

    def test_response_field_type_conflict(self) -> None:
        report = check_consistency(
            [
                _backend(response_fields={"id": "int"}),
                _frontend(response_fields={"id": "str"}),
            ]
        )
        issues = report.issues_by_type(ConsistencyIssueType.FIELD_TYPE_MISMATCH)
        assert len(issues) == 1

    def test_matching_types_no_issue(self) -> None:
        report = check_consistency(
            [
                _backend(request_fields={"count": "int"}, response_fields={"ok": "bool"}),
                _frontend(request_fields={"count": "int"}, response_fields={"ok": "bool"}),
            ]
        )
        assert report.issues_by_type(ConsistencyIssueType.FIELD_TYPE_MISMATCH) == []

    def test_empty_type_annotation_skipped(self) -> None:
        # Empty string type → no type conflict reported
        report = check_consistency(
            [
                _backend(request_fields={"value": "int"}),
                _frontend(request_fields={"value": ""}),
            ]
        )
        assert report.issues_by_type(ConsistencyIssueType.FIELD_TYPE_MISMATCH) == []


# ---------------------------------------------------------------------------
# Error code alignment
# ---------------------------------------------------------------------------


class TestErrorCodeAlignment:
    def test_consumer_handles_undeclared_error_code(self) -> None:
        report = check_consistency(
            [
                _backend(error_codes=[400, 422]),
                _frontend(error_codes=[400, 422, 503]),
            ]
        )
        issues = report.issues_by_type(ConsistencyIssueType.UNHANDLED_ERROR_CODE)
        assert len(issues) == 1
        assert "503" in issues[0].description

    def test_consumer_subset_of_producer_codes_passes(self) -> None:
        report = check_consistency(
            [
                _backend(error_codes=[400, 422, 500]),
                _frontend(error_codes=[400, 422]),
            ]
        )
        assert report.issues_by_type(ConsistencyIssueType.UNHANDLED_ERROR_CODE) == []

    def test_empty_error_codes_skips_check(self) -> None:
        report = check_consistency(
            [
                _backend(error_codes=[]),
                _frontend(error_codes=[404]),
            ]
        )
        assert report.issues_by_type(ConsistencyIssueType.UNHANDLED_ERROR_CODE) == []

    def test_multiple_undeclared_codes(self) -> None:
        report = check_consistency(
            [
                _backend(error_codes=[400]),
                _frontend(error_codes=[400, 429, 503]),
            ]
        )
        issues = report.issues_by_type(ConsistencyIssueType.UNHANDLED_ERROR_CODE)
        assert len(issues) == 2


# ---------------------------------------------------------------------------
# Multi-agent / multi-endpoint scenarios
# ---------------------------------------------------------------------------


class TestMultiAgentScenarios:
    def test_three_agents_consistent(self) -> None:
        backend = AgentImplementation(
            agent_id="be",
            role="backend",
            contracts=[
                ApiContract(
                    endpoint="/api/v1/orders",
                    method="POST",
                    request_fields={"item_id": "str", "qty": "int"},
                    response_fields={"order_id": "str", "status": "str"},
                    error_codes=[400, 422, 500],
                )
            ],
        )
        frontend = AgentImplementation(
            agent_id="fe",
            role="frontend",
            contracts=[
                ApiContract(
                    endpoint="/api/v1/orders",
                    method="POST",
                    request_fields={"item_id": "str", "qty": "int"},
                    response_fields={"order_id": "str"},
                    error_codes=[400, 422],
                )
            ],
        )
        qa = AgentImplementation(
            agent_id="qa",
            role="qa",
            contracts=[
                ApiContract(
                    endpoint="/api/v1/orders",
                    method="POST",
                    request_fields={"item_id": "str"},
                    response_fields={"order_id": "str"},
                    error_codes=[400],
                )
            ],
        )
        report = check_consistency([backend, frontend, qa])
        assert report.is_consistent

    def test_multiple_endpoints_one_broken(self) -> None:
        b = AgentImplementation(
            agent_id="b",
            role="backend",
            contracts=[
                ApiContract(endpoint="/api/items", method="GET", response_fields={"items": "list"}),
                ApiContract(endpoint="/api/orders", method="POST", response_fields={"id": "str"}),
            ],
        )
        f = AgentImplementation(
            agent_id="f",
            role="frontend",
            contracts=[
                # /api/items is fine
                ApiContract(endpoint="/api/items", method="GET", response_fields={"items": "list"}),
                # /api/orders has a missing field
                ApiContract(
                    endpoint="/api/orders",
                    method="POST",
                    response_fields={"id": "str", "missing_field": "str"},
                ),
            ],
        )
        report = check_consistency([b, f])
        assert not report.is_consistent
        assert report.checked_endpoints == 2
        missing = report.issues_by_type(ConsistencyIssueType.MISSING_RESPONSE_FIELD)
        assert len(missing) == 1
        assert "missing_field" in missing[0].description

    def test_backend_elected_as_producer_over_other_roles(self) -> None:
        """Backend role is always the producer even if listed second."""
        frontend_first = AgentImplementation(
            agent_id="fe",
            role="frontend",
            contracts=[ApiContract(endpoint="/x", method="GET", response_fields={"id": "str", "extra": "str"})],
        )
        backend_second = AgentImplementation(
            agent_id="be",
            role="backend",
            contracts=[ApiContract(endpoint="/x", method="GET", response_fields={"id": "str"})],
        )
        # Frontend requests "extra" but backend doesn't supply it → issue
        report = check_consistency([frontend_first, backend_second])
        assert not report.is_consistent
        missing = report.issues_by_type(ConsistencyIssueType.MISSING_RESPONSE_FIELD)
        assert any("extra" in i.description for i in missing)

    def test_no_backend_role_uses_first_as_producer(self) -> None:
        """When no backend agent exists, first agent acts as producer."""
        a1 = AgentImplementation(
            agent_id="a1",
            role="frontend",
            contracts=[ApiContract(endpoint="/y", method="GET", response_fields={"val": "str"})],
        )
        a2 = AgentImplementation(
            agent_id="a2",
            role="qa",
            contracts=[ApiContract(endpoint="/y", method="GET", response_fields={"val": "str", "extra": "int"})],
        )
        report = check_consistency([a1, a2])
        missing = report.issues_by_type(ConsistencyIssueType.MISSING_RESPONSE_FIELD)
        assert len(missing) == 1
        assert "extra" in missing[0].description


# ---------------------------------------------------------------------------
# ConsistencyReport helpers
# ---------------------------------------------------------------------------


class TestConsistencyReportHelpers:
    def test_issues_by_type_filters_correctly(self) -> None:
        report = check_consistency(
            [
                _backend(response_fields={"id": "str"}, error_codes=[400]),
                _frontend(response_fields={"id": "str", "missing": "str"}, error_codes=[400, 503]),
            ]
        )
        missing = report.issues_by_type(ConsistencyIssueType.MISSING_RESPONSE_FIELD)
        unhandled = report.issues_by_type(ConsistencyIssueType.UNHANDLED_ERROR_CODE)
        assert len(missing) >= 1
        assert len(unhandled) >= 1

    def test_is_consistent_false_when_issues_exist(self) -> None:
        report = check_consistency(
            [
                _backend(response_fields={}),
                _frontend(response_fields={"id": "str"}),
            ]
        )
        assert not report.is_consistent

    def test_checked_endpoints_counts_shared_only(self) -> None:
        b = AgentImplementation(
            agent_id="b",
            role="backend",
            contracts=[
                ApiContract(endpoint="/shared", method="GET"),
                ApiContract(endpoint="/backend-only", method="POST"),
            ],
        )
        f = AgentImplementation(
            agent_id="f",
            role="frontend",
            contracts=[
                ApiContract(endpoint="/shared", method="GET"),
                ApiContract(endpoint="/frontend-only", method="PUT"),
            ],
        )
        report = check_consistency([b, f])
        assert report.checked_endpoints == 1
