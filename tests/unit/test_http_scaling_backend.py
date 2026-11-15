"""Contract tests for the opt-in external HTTP scaling backend."""

from __future__ import annotations

import httpx
import pytest
import respx

from bernstein.core.protocols.cluster.http_scaling_backend import HTTPScalingBackend, HTTPScalingConfig


def test_env_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SCALING_URL", "https://fleet.example.test/")
    monkeypatch.setenv("BERNSTEIN_SCALING_TOKEN", "test-token")
    assert HTTPScalingConfig.from_env() == HTTPScalingConfig(
        base_url="https://fleet.example.test", auth_token="test-token"
    )


def test_unconfigured_backend_performs_no_io() -> None:
    backend = HTTPScalingBackend(HTTPScalingConfig())
    assert backend.name == "http"
    assert backend.current_node_count() == 0
    result = backend.scale_to(4)
    assert not result.success
    assert result.error == "no base_url configured"


@respx.mock
def test_status_and_scale_contract() -> None:
    backend = HTTPScalingBackend(HTTPScalingConfig(base_url="https://fleet.example.test", auth_token="test-token"))
    status = respx.get("https://fleet.example.test/api/cluster/autoscale/status").mock(
        return_value=httpx.Response(200, json={"current": 2, "target": 2})
    )
    scale = respx.patch("https://fleet.example.test/api/cluster/autoscale/config").mock(
        return_value=httpx.Response(200, json={"target": 5})
    )
    result = backend.scale_to(5)
    assert result.success
    assert (result.previous_count, result.new_count, result.backend) == (2, 5, "http")
    assert status.call_count == 1 and scale.call_count == 1
    request = scale.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["Content-Type"] == "application/json"
    assert request.content == b'{"target":5}'


@respx.mock
def test_status_without_token() -> None:
    backend = HTTPScalingBackend(HTTPScalingConfig(base_url="https://fleet.example.test"))
    route = respx.get("https://fleet.example.test/api/cluster/autoscale/status").mock(
        return_value=httpx.Response(200, json={"current": 3})
    )
    assert backend.current_node_count() == 3
    assert "Authorization" not in route.calls[0].request.headers


@respx.mock
def test_status_http_failure_returns_zero() -> None:
    backend = HTTPScalingBackend(HTTPScalingConfig(base_url="https://fleet.example.test"))
    respx.get("https://fleet.example.test/api/cluster/autoscale/status").mock(return_value=httpx.Response(503))
    assert backend.current_node_count() == 0


@respx.mock
def test_failed_scale_preserves_previous_count() -> None:
    backend = HTTPScalingBackend(HTTPScalingConfig(base_url="https://fleet.example.test"))
    respx.get("https://fleet.example.test/api/cluster/autoscale/status").mock(
        return_value=httpx.Response(200, json={"current": 3})
    )
    respx.patch("https://fleet.example.test/api/cluster/autoscale/config").mock(return_value=httpx.Response(503))
    result = backend.scale_to(5)
    assert not result.success
    assert result.previous_count == result.new_count == 3
    assert result.error


@respx.mock
def test_transport_failure_returns_failed_result() -> None:
    backend = HTTPScalingBackend(HTTPScalingConfig(base_url="https://fleet.example.test"))
    respx.get("https://fleet.example.test/api/cluster/autoscale/status").mock(
        return_value=httpx.Response(200, json={"current": 2})
    )
    respx.patch("https://fleet.example.test/api/cluster/autoscale/config").mock(
        side_effect=httpx.ConnectError("offline")
    )
    result = backend.scale_to(4)
    assert not result.success and result.new_count == 2
