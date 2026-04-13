"""Tests for MCP-008: MCP elicitation handling."""

from __future__ import annotations

import time

import pytest
from bernstein.core.mcp_elicitation import (
    AutoResolvePolicy,
    ElicitationHandler,
    ElicitationRequest,
    ElicitationStatus,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def handler() -> ElicitationHandler:
    return ElicitationHandler(default_timeout=5.0)


@pytest.fixture()
def confirm_request() -> ElicitationRequest:
    return ElicitationRequest(
        server_name="github",
        message="Confirm delete branch 'old-feature'?",
        request_type="confirmation",
    )


@pytest.fixture()
def input_request() -> ElicitationRequest:
    return ElicitationRequest(
        server_name="database",
        message="Enter migration version number:",
        request_type="input",
        schema={"type": "string"},
    )


# ---------------------------------------------------------------------------
# Tests — ElicitationRequest
# ---------------------------------------------------------------------------


class TestElicitationRequest:
    def test_auto_generates_id(self) -> None:
        req = ElicitationRequest(server_name="test", message="hello")
        assert req.id != ""
        assert len(req.id) == 12

    def test_to_dict(self, confirm_request: ElicitationRequest) -> None:
        d = confirm_request.to_dict()
        assert d["server_name"] == "github"
        assert d["status"] == "pending"
        assert d["request_type"] == "confirmation"


# ---------------------------------------------------------------------------
# Tests — AutoResolvePolicy
# ---------------------------------------------------------------------------


class TestAutoResolvePolicy:
    def test_matches_pattern(self) -> None:
        policy = AutoResolvePolicy(name="test", pattern="confirm.*delete", response="yes")
        req = ElicitationRequest(message="Confirm delete branch?", request_type="confirmation")
        assert policy.matches(req) is True

    def test_no_match(self) -> None:
        policy = AutoResolvePolicy(name="test", pattern="confirm.*delete", response="yes")
        req = ElicitationRequest(message="Enter name:", request_type="input")
        assert policy.matches(req) is False

    def test_filter_by_request_type(self) -> None:
        policy = AutoResolvePolicy(
            name="test",
            pattern="confirm",
            response="yes",
            request_types=("confirmation",),
        )
        req_match = ElicitationRequest(message="confirm action", request_type="confirmation")
        req_no = ElicitationRequest(message="confirm action", request_type="input")
        assert policy.matches(req_match) is True
        assert policy.matches(req_no) is False

    def test_filter_by_server_name(self) -> None:
        policy = AutoResolvePolicy(
            name="test",
            pattern=".*",
            response="yes",
            server_names=("github",),
        )
        req_match = ElicitationRequest(message="test", server_name="github")
        req_no = ElicitationRequest(message="test", server_name="gitlab")
        assert policy.matches(req_match) is True
        assert policy.matches(req_no) is False


# ---------------------------------------------------------------------------
# Tests — ElicitationHandler auto-resolve
# ---------------------------------------------------------------------------


class TestHandlerAutoResolve:
    def test_auto_resolve_matching_policy(
        self, handler: ElicitationHandler, confirm_request: ElicitationRequest
    ) -> None:
        handler.add_auto_policy("confirm_delete", pattern="confirm.*delete", response="yes")
        result = handler.handle(confirm_request)
        assert result.status == ElicitationStatus.AUTO_RESOLVED
        assert result.response == "yes"
        assert result.resolved_by == "auto:confirm_delete"

    def test_no_matching_policy_queues_pending(
        self, handler: ElicitationHandler, input_request: ElicitationRequest
    ) -> None:
        handler.add_auto_policy("confirm_delete", pattern="confirm.*delete", response="yes")
        result = handler.handle(input_request)
        assert result.status == ElicitationStatus.PENDING
        assert len(handler.get_pending()) == 1

    def test_multiple_policies_first_wins(self, handler: ElicitationHandler) -> None:
        handler.add_auto_policy("p1", pattern="confirm", response="first")
        handler.add_auto_policy("p2", pattern="confirm", response="second")
        req = ElicitationRequest(message="confirm action")
        result = handler.handle(req)
        assert result.response == "first"


# ---------------------------------------------------------------------------
# Tests — ElicitationHandler user resolve
# ---------------------------------------------------------------------------


class TestHandlerUserResolve:
    def test_resolve_pending(self, handler: ElicitationHandler, input_request: ElicitationRequest) -> None:
        handler.handle(input_request)
        result = handler.resolve(input_request.id, "42")
        assert result is not None
        assert result.status == ElicitationStatus.USER_RESOLVED
        assert result.response == "42"
        assert handler.get_pending() == []

    def test_resolve_nonexistent(self, handler: ElicitationHandler) -> None:
        assert handler.resolve("nonexistent", "value") is None

    def test_deny_pending(self, handler: ElicitationHandler, input_request: ElicitationRequest) -> None:
        handler.handle(input_request)
        result = handler.deny(input_request.id)
        assert result is not None
        assert result.status == ElicitationStatus.DENIED

    def test_deny_nonexistent(self, handler: ElicitationHandler) -> None:
        assert handler.deny("nonexistent") is None


# ---------------------------------------------------------------------------
# Tests — Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_expire_timed_out(self, handler: ElicitationHandler) -> None:
        # Create request with old timestamp
        req = ElicitationRequest(
            server_name="test",
            message="stale request",
            created_at=time.time() - 100,
        )
        handler.handle(req)
        expired = handler.expire_timed_out()
        assert len(expired) == 1
        assert expired[0].status == ElicitationStatus.TIMED_OUT

    def test_non_expired_not_timed_out(self, handler: ElicitationHandler, input_request: ElicitationRequest) -> None:
        handler.handle(input_request)
        expired = handler.expire_timed_out()
        assert len(expired) == 0


# ---------------------------------------------------------------------------
# Tests — Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_to_dict(self, handler: ElicitationHandler, input_request: ElicitationRequest) -> None:
        handler.add_auto_policy("p1", pattern=".*", response="auto")
        handler.handle(input_request)
        d = handler.to_dict()
        assert d["policy_count"] == 1
        # The request matched p1, so it should be in resolved, not pending
        assert d["resolved_count"] == 1

    def test_get_resolved(self, handler: ElicitationHandler, confirm_request: ElicitationRequest) -> None:
        handler.add_auto_policy("p1", pattern="confirm", response="yes")
        handler.handle(confirm_request)
        resolved = handler.get_resolved()
        assert len(resolved) == 1
        assert resolved[0].response == "yes"
