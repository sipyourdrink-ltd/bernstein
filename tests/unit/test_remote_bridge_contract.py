"""Observed remote outcomes remain distinct from legacy bridge behavior."""

from __future__ import annotations

import copy
import importlib
from dataclasses import FrozenInstanceError
from enum import StrEnum
from typing import Any

import pytest

from bernstein.bridges.base import AgentState, AgentStatus, BridgeConfig, RuntimeBridge, SpawnRequest


def _contract() -> Any:
    module = importlib.import_module("bernstein.bridges.base")
    for name in (
        "RemoteRunState",
        "RemoteCancelOutcome",
        "RemoteRunHandle",
        "RemoteRunStatus",
        "RemoteCancel",
        "BridgeCapabilityError",
    ):
        assert hasattr(module, name), f"missing remote observation contract: {name}"
    return module


class LegacyBridge(RuntimeBridge):
    """An existing concrete implementation need not implement new capabilities."""

    def __init__(self) -> None:
        super().__init__(BridgeConfig(bridge_type="legacy", endpoint="unused"))
        self.calls: list[str] = []

    async def spawn(self, request: SpawnRequest) -> AgentStatus:
        self.calls.append("spawn")
        return AgentStatus(request.agent_id, AgentState.RUNNING)

    async def status(self, agent_id: str) -> AgentStatus:
        self.calls.append("status")
        return AgentStatus(agent_id, AgentState.COMPLETED)

    async def cancel(self, agent_id: str) -> None:
        self.calls.append("cancel")

    async def logs(self, agent_id: str, *, tail: int | None = None) -> bytes:
        self.calls.append("logs")
        return b"legacy"

    def name(self) -> str:
        self.calls.append("name")
        return "legacy"


def _handle(**changes: Any) -> Any:
    c = _contract()
    return c.RemoteRunHandle(
        **{"service": "scripted", "remote_run_id": "run-a", "adapter_version": "1", "submitted_at": 0, **changes}
    )


@pytest.mark.asyncio
async def test_optional_capability_refusal_never_calls_legacy_bridge() -> None:
    bridge = LegacyBridge()
    assert await bridge.cancel("local-agent") is None
    assert bridge.calls == ["cancel"]
    bridge.calls.clear()
    c = _contract()
    assert issubclass(c.BridgeCapabilityError, c.BridgeError)
    handle = _handle()
    for capability in ("remote_status", "cancel_remote"):
        method = getattr(bridge, capability)
        with pytest.raises(c.BridgeCapabilityError) as refused:
            await method(handle)
        assert refused.value.capability == capability
        assert capability in str(refused.value)
        assert refused.value.agent_id is None
        assert bridge.calls == []
    assert await bridge.status("local-agent") == AgentStatus("local-agent", AgentState.COMPLETED)
    assert {s.value for s in AgentState} == {"pending", "running", "completed", "failed", "cancelled"}


def test_neutral_state_vocabulary_and_observed_terminality() -> None:
    c = _contract()
    assert {state.value for state in c.RemoteRunState} == {
        "submitted",
        "working",
        "input-required",
        "auth-required",
        "completed",
        "failed",
        "canceled",
        "unknown",
    }
    assert {outcome.value for outcome in c.RemoteCancelOutcome} == {"confirmed", "unconfirmed", "unknown"}
    terminal = {state.value for state in c.RemoteRunState if state.is_terminal}
    assert terminal == {"completed", "failed", "canceled"}
    for name in ("SUBMITTED", "WORKING", "INPUT_REQUIRED", "AUTH_REQUIRED", "UNKNOWN"):
        assert not getattr(c.RemoteRunState, name).is_terminal


@pytest.mark.parametrize("field", ["service", "remote_run_id", "adapter_version", "console_url"])
@pytest.mark.parametrize("value", ["", " ", 3, False, {}, []])
def test_handle_rejects_malformed_identity(field: str, value: object) -> None:
    _contract()
    with pytest.raises((TypeError, ValueError)):
        _handle(**{field: value})


def test_handle_identity_is_preserved_without_coercion_and_is_frozen() -> None:
    handle = _handle(service=" supplied identity ", submitted_at=12, console_url="https://console.invalid/run-a")
    assert handle.service == " supplied identity " and type(handle.submitted_at) is int
    assert handle.console_url == "https://console.invalid/run-a"
    assert handle != _handle(
        remote_run_id="run-b", service=handle.service, submitted_at=12, console_url=handle.console_url
    )
    assert handle != _handle(
        adapter_version="2", service=handle.service, submitted_at=12, console_url=handle.console_url
    )
    assert copy.deepcopy(handle) == handle
    with pytest.raises(FrozenInstanceError):
        handle.remote_run_id = "other"


@pytest.mark.parametrize(
    "value", [True, False, -1, -0.1, float("nan"), float("inf"), -float("inf"), "1", None, {}, 10**1000]
)
def test_remote_timestamps_are_finite_nonnegative_epoch_seconds(value: object) -> None:
    c = _contract()
    with pytest.raises((TypeError, ValueError)):
        _handle(submitted_at=value)
    for dto, kind in ((c.RemoteRunStatus, c.RemoteRunState.UNKNOWN), (c.RemoteCancel, c.RemoteCancelOutcome.UNKNOWN)):
        with pytest.raises((TypeError, ValueError)):
            dto(_handle(), kind, value)


class _OtherState(StrEnum):
    UNKNOWN = "unknown"


@pytest.mark.parametrize("value", ["unknown", "completed", "future", None, 3, {}, _OtherState.UNKNOWN])
def test_observation_requires_its_declared_enum(value: object) -> None:
    c = _contract()
    for dto in (c.RemoteRunStatus, c.RemoteCancel):
        with pytest.raises((TypeError, ValueError)):
            dto(_handle(), value, 1)


@pytest.mark.parametrize("value", [None, {}, "run-a", AgentStatus("a", AgentState.RUNNING)])
def test_observation_requires_full_typed_handle(value: object) -> None:
    c = _contract()
    for dto, kind in ((c.RemoteRunStatus, c.RemoteRunState.UNKNOWN), (c.RemoteCancel, c.RemoteCancelOutcome.UNKNOWN)):
        with pytest.raises((TypeError, ValueError)):
            dto(value, kind, 1)


def test_frozen_observation_binding_and_detail_redaction() -> None:
    c = _contract()
    handle = _handle()
    for dto, kind in (
        (c.RemoteRunStatus, c.RemoteRunState.UNKNOWN),
        (c.RemoteCancel, c.RemoteCancelOutcome.UNCONFIRMED),
    ):
        observation = dto(handle, kind, 1.5, detail="private observation text")
        assert observation.handle is handle and observation.detail == "private observation text"
        assert "private observation text" not in repr(observation)
        assert copy.deepcopy(observation) == observation
        with pytest.raises(FrozenInstanceError):
            observation.handle = _handle(remote_run_id="other")
        with pytest.raises((TypeError, ValueError)):
            dto(handle, kind, 1, detail={"mutable": "detail"})
    with pytest.raises((TypeError, ValueError)):
        c.RemoteRunStatus(handle, c.RemoteCancelOutcome.UNKNOWN, 1)
    with pytest.raises((TypeError, ValueError)):
        c.RemoteCancel(handle, c.RemoteRunState.UNKNOWN, 1)


@pytest.mark.asyncio
async def test_scripted_in_process_observations_keep_unknown_and_unconfirmed_distinct() -> None:
    c = _contract()

    class ScriptedBridge(LegacyBridge):
        def __init__(self) -> None:
            super().__init__()
            self.states = iter((c.RemoteRunState.UNKNOWN, c.RemoteRunState.INPUT_REQUIRED, c.RemoteRunState.COMPLETED))
            self.outcomes = iter(
                (c.RemoteCancelOutcome.UNCONFIRMED, c.RemoteCancelOutcome.UNKNOWN, c.RemoteCancelOutcome.CONFIRMED)
            )

        async def remote_status(self, handle: Any) -> Any:
            return c.RemoteRunStatus(handle, next(self.states), 2)

        async def cancel_remote(self, handle: Any) -> Any:
            return c.RemoteCancel(handle, next(self.outcomes), 3)

    bridge = ScriptedBridge()
    handle = _handle()
    statuses = [await bridge.remote_status(handle) for _ in range(3)]
    cancellations = [await bridge.cancel_remote(handle) for _ in range(3)]
    assert [s.state.value for s in statuses] == ["unknown", "input-required", "completed"]
    assert [s.state.is_terminal for s in statuses] == [False, False, True]
    assert [c.outcome.value for c in cancellations] == ["unconfirmed", "unknown", "confirmed"]
    assert all(result.handle is handle for result in [*statuses, *cancellations])
    assert bridge.calls == []
