"""Tests for ENT-011: IP allowlisting for API access."""

from __future__ import annotations

import logging

import pytest
from bernstein.core.ip_allowlist import (
    check_ip_allowed,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from bernstein.core.security.ip_allowlist import (
    IPAllowlistMiddleware,
    _parse_allowed_networks,
    allowlist_is_unusable,
)

# ---------------------------------------------------------------------------
# check_ip_allowed (exercises network parsing internally)
# ---------------------------------------------------------------------------


class TestCheckIPAllowed:
    def test_localhost_always_allowed(self) -> None:
        assert check_ip_allowed("127.0.0.1", [])
        assert check_ip_allowed("::1", [])
        assert check_ip_allowed("localhost", [])

    def test_ip_in_range(self) -> None:
        assert check_ip_allowed("10.0.0.5", ["10.0.0.0/8"])

    def test_ip_not_in_range(self) -> None:
        assert not check_ip_allowed("192.168.1.1", ["10.0.0.0/8"])

    def test_exact_ip_match(self) -> None:
        assert check_ip_allowed("10.0.0.1", ["10.0.0.1/32"])
        assert not check_ip_allowed("10.0.0.2", ["10.0.0.1/32"])

    def test_multiple_ranges(self) -> None:
        ranges = ["10.0.0.0/8", "172.16.0.0/12"]
        assert check_ip_allowed("10.1.2.3", ranges)
        assert check_ip_allowed("172.16.5.1", ranges)
        assert not check_ip_allowed("192.168.1.1", ranges)

    def test_invalid_ip_rejected(self) -> None:
        assert not check_ip_allowed("not-an-ip", ["10.0.0.0/8"])

    def test_ipv6_in_range(self) -> None:
        assert check_ip_allowed("fd00::1", ["fd00::/8"])

    def test_single_ip_cidr(self) -> None:
        assert check_ip_allowed("10.0.0.1", ["10.0.0.1"])

    def test_empty_allowlist_denies_non_localhost(self) -> None:
        assert not check_ip_allowed("10.0.0.1", [])

    def test_invalid_cidr_ignored(self) -> None:
        # Invalid CIDR should not match, but valid ones still work
        assert check_ip_allowed("10.0.0.1", ["invalid", "10.0.0.0/8"])
        assert not check_ip_allowed("192.168.1.1", ["invalid"])


# ---------------------------------------------------------------------------
# IPAllowlistMiddleware: a configured allowlist that parses to nothing
# ---------------------------------------------------------------------------
#
# ``_parse_allowed_networks`` drops a range it cannot parse. That is the right
# call while another range survives - the allowlist gets narrower, which errs
# towards denial. When *every* range is dropped the same behaviour inverts:
# the middleware sees an empty tuple, reads it as "no allowlist configured",
# and passes every request through. One typo in one CIDR is enough, and
# nothing in the response says the restriction stopped applying.
#
# ``check_ip_allowed`` never had this problem - an empty parse there means no
# network matches, so it denies (``test_invalid_cidr_ignored`` above pins it).
# These tests pin the middleware to the same answer.


def _client(allowed_ips: list[str] | None) -> TestClient:
    """A one-route app behind the middleware, addressed from a public IP."""

    async def handler(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("reached the handler")

    app = Starlette(routes=[Route("/tasks", handler), Route("/health", handler)])
    app.add_middleware(IPAllowlistMiddleware, allowed_ips=allowed_ips)
    # 203.0.113.0/24 is TEST-NET-3: routable-looking, and not loopback, so the
    # request is judged by the allowlist rather than by the loopback exemption.
    return TestClient(app, client=("203.0.113.9", 1234))


class TestUnusableAllowlistIsNotAnAbsentOne:
    def test_a_sole_unparseable_range_denies_rather_than_passing_through(self) -> None:
        """The whole bug in one call: /33 is not a prefix length."""
        response = _client(["10.0.0.0/33"]).get("/tasks")
        assert response.status_code == 500
        assert "unusable" in response.json()["detail"]

    def test_every_range_unparseable_denies(self) -> None:
        response = _client(["not-an-ip", "10.0.0.0/33", ""]).get("/tasks")
        assert response.status_code == 500

    def test_one_surviving_range_still_enforces_that_range(self) -> None:
        """Dropping narrows: the good half of a half-broken list still applies."""
        response = _client(["10.0.0.0/33", "10.0.0.0/8"]).get("/tasks")
        assert response.status_code == 403
        assert "203.0.113.9" in response.json()["detail"]

    def test_one_surviving_range_still_admits_a_client_inside_it(self) -> None:
        async def handler(_request: Request) -> PlainTextResponse:
            return PlainTextResponse("reached the handler")

        app = Starlette(routes=[Route("/tasks", handler)])
        app.add_middleware(IPAllowlistMiddleware, allowed_ips=["garbage", "10.0.0.0/8"])
        response = TestClient(app, client=("10.1.2.3", 1234)).get("/tasks")
        assert response.status_code == 200

    def test_no_allowlist_configured_still_passes_through(self) -> None:
        """``None`` means the operator asked for no restriction at all."""
        assert _client(None).get("/tasks").status_code == 200

    def test_an_explicitly_empty_allowlist_still_passes_through(self) -> None:
        """``[]`` is how callers spell "feature off"; unchanged on purpose.

        It is not the bug: an empty list carries no operator intent that got
        lost. Only a non-empty list that parses to nothing does.
        """
        assert _client([]).get("/tasks").status_code == 200

    def test_public_paths_stay_reachable_so_health_checks_survive(self) -> None:
        """A refused allowlist must not take the liveness probe with it.

        Otherwise a typo in a CIDR gets the container killed by its own
        orchestrator before an operator can read the log line naming it.
        """
        assert _client(["10.0.0.0/33"]).get("/health").status_code == 200

    def test_the_refusal_names_the_cause_in_the_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """A 500 with no explanation is a worse failure than the fail-open."""
        with caplog.at_level(logging.ERROR, logger="bernstein.core.security.ip_allowlist"):
            _client(["10.0.0.0/33"]).get("/tasks")
        assert "none of them parseable" in caplog.text


class TestAllowlistIsUnusable:
    """The predicate on its own - it is the whole distinction."""

    def test_configured_and_parsed_is_usable(self) -> None:
        assert not allowlist_is_unusable(["10.0.0.0/8"], _parse_allowed_networks(["10.0.0.0/8"]))

    def test_configured_and_unparsed_is_unusable(self) -> None:
        assert allowlist_is_unusable(["10.0.0.0/33"], _parse_allowed_networks(["10.0.0.0/33"]))

    def test_unconfigured_is_not_unusable(self) -> None:
        """Nothing was asked for, so nothing was lost."""
        assert not allowlist_is_unusable([], ())


# ---------------------------------------------------------------------------
# Log injection through the forwarded-IP header
# ---------------------------------------------------------------------------


def test_a_forwarded_ip_cannot_forge_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """The blocked-request line is built from two request-controlled values.

    ``X-Forwarded-For`` is trusted only when the direct peer is loopback, but
    that is exactly the deployment this middleware is written for - a proxy on
    the same host. The header value then reaches ``logger.warning`` verbatim,
    so a newline in it writes the attacker's own line into the log.
    """

    async def handler(_request: Request) -> PlainTextResponse:  # pragma: no cover - never reached
        return PlainTextResponse("reached the handler")

    app = Starlette(routes=[Route("/tasks", handler)])
    app.add_middleware(IPAllowlistMiddleware, allowed_ips=["10.0.0.0/8"])
    client = TestClient(app, client=("127.0.0.1", 1234))

    with caplog.at_level(logging.WARNING, logger="bernstein.core.security.ip_allowlist"):
        client.get("/tasks", headers={"X-Forwarded-For": "9.9.9.9\nINFO Blocked request from IP nobody"})

    assert "\nINFO" not in caplog.text
    assert "\n" in caplog.text
