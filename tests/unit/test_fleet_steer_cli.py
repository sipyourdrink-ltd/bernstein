"""``bernstein fleet steer`` CLI tests (#2508).

The command computes the confirmed payload hash locally and posts it to the
task server, so the receipt binds exactly the command it printed. These tests
exercise the payload it sends and the errors it surfaces without touching the
network (the HTTP call is monkeypatched).
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import fleet_cmd
from bernstein.core.orchestration.steering import SteeringCommand


@pytest.fixture()
def captured_post(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_post(server_url: str, token: str | None, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append({"server_url": server_url, "token": token, "task_id": task_id, "payload": payload})
        return {
            "kind": payload["kind"],
            "task_id": task_id,
            "receipt_hash": "hmac-sha256:abc123",
            "mailbox_seq": 0,
        }

    monkeypatch.setattr(fleet_cmd, "_post_steer", _fake_post)
    return calls


def test_steer_sends_confirmed_payload_hash(captured_post: list[dict[str, Any]]) -> None:
    runner = CliRunner()
    result = runner.invoke(
        fleet_cmd.steer_cmd,
        ["task-1", "guidance", "--guidance", "stop refactoring, fix the failing test"],
    )
    assert result.exit_code == 0, result.output
    assert len(captured_post) == 1
    payload = captured_post[0]["payload"]
    assert payload["kind"] == "guidance"
    assert payload["guidance"] == "stop refactoring, fix the failing test"
    # The confirmed hash equals the hash of the exact command the CLI built.
    expected = SteeringCommand(
        kind="guidance", task_id="task-1", principal="cli-operator", guidance="stop refactoring, fix the failing test"
    ).payload_hash()
    assert payload["displayed_payload_hash"] == expected
    assert "Steered" in result.output


def test_steer_rejects_malformed_command_before_posting(captured_post: list[dict[str, Any]]) -> None:
    runner = CliRunner()
    # guidance kind with no guidance text fails local validation.
    result = runner.invoke(fleet_cmd.steer_cmd, ["task-1", "guidance"])
    assert result.exit_code != 0
    assert captured_post == []


def test_steer_abort_requires_session(captured_post: list[dict[str, Any]]) -> None:
    runner = CliRunner()
    result = runner.invoke(fleet_cmd.steer_cmd, ["task-1", "abort"])
    assert result.exit_code != 0
    assert captured_post == []


def test_steer_surfaces_server_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def _raise(server_url: str, token: str | None, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = httpx.Request("POST", f"{server_url}/tasks/{task_id}/steer")
        response = httpx.Response(403, text="scope 'viewer' is not authorised to steer", request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(fleet_cmd, "_post_steer", _raise)
    runner = CliRunner()
    result = runner.invoke(fleet_cmd.steer_cmd, ["task-1", "abort", "--session-id", "sess-1"])
    assert result.exit_code != 0
    assert "403" in result.output


def test_steer_emits_json_receipt(captured_post: list[dict[str, Any]]) -> None:
    runner = CliRunner()
    result = runner.invoke(
        fleet_cmd.steer_cmd,
        ["task-1", "redirect", "--redirect-target", "ship the hotfix", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert "receipt_hash" in result.output


def test_steer_unknown_kind_is_rejected() -> None:
    runner = CliRunner()
    result = runner.invoke(fleet_cmd.steer_cmd, ["task-1", "bogus"])
    assert result.exit_code != 0
