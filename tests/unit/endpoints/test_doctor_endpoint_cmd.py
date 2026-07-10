"""``bernstein doctor --endpoint`` CLI tests (issue #2356).

AC coverage:

* AC2 -- the doctor deterministically certifies or rejects an endpoint per
  role, with reasons, and the result is a signed receipt anchored to the
  chain rather than a boolean in config.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from click.testing import CliRunner

from bernstein.cli.main import cli
from tests.unit.endpoints.stub_endpoint import EndpointBehavior, stub_endpoint_server

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _invoke(args: list[str]) -> object:
    return CliRunner().invoke(cli, args)


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


def test_doctor_endpoint_certifies_local_tier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    with stub_endpoint_server() as base_url:
        result = _invoke(["doctor", "--endpoint", base_url, "--endpoint-model", "tiny-coder"])
    assert result.exit_code == 0, result.output
    assert "linter" in result.output
    assert "certified" in result.output

    receipts = list((tmp_path / ".sdd" / "endpoints" / "certifications").glob("*.json"))
    assert len(receipts) == 1


def test_doctor_endpoint_rejects_with_reasons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    with stub_endpoint_server(EndpointBehavior(tools_ok=False)) as base_url:
        result = _invoke(
            [
                "doctor",
                "--endpoint",
                base_url,
                "--endpoint-model",
                "tiny-coder",
                "--role",
                "test_writer",
            ]
        )
    assert result.exit_code == 1
    assert "rejected" in result.output
    assert "no_tool_call" in result.output


def test_doctor_endpoint_json_output_is_machine_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    with stub_endpoint_server() as base_url:
        result = _invoke(["doctor", "--endpoint", base_url, "--endpoint-model", "tiny-coder", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["model"] == "tiny-coder"
    assert payload["transcript_hash"].startswith("sha256:")
    verdicts = {v["role"]: v for v in payload["verdicts"]}
    assert verdicts["linter"]["certified"] is True
    assert payload["fingerprint"]
    assert payload["journal_entry_hash"]


def test_doctor_endpoint_discovers_model_when_omitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    with stub_endpoint_server(EndpointBehavior(model="listed-model")) as base_url:
        result = _invoke(["doctor", "--endpoint", base_url, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["model"] == "listed-model"


def test_doctor_endpoint_fails_usage_when_model_undiscoverable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    with stub_endpoint_server(EndpointBehavior(models_ok=False)) as base_url:
        result = _invoke(["doctor", "--endpoint", base_url])
    assert result.exit_code == 2
    assert "--endpoint-model" in result.output


def test_doctor_endpoint_verdicts_are_deterministic_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(tmp_path, monkeypatch)
    behavior = EndpointBehavior(tools_ok=False)
    with stub_endpoint_server(behavior) as base_url:
        first = _invoke(["doctor", "--endpoint", base_url, "--endpoint-model", "tiny-coder", "--json"])
        second = _invoke(["doctor", "--endpoint", base_url, "--endpoint-model", "tiny-coder", "--json"])
    assert first.exit_code == second.exit_code
    a = json.loads(first.output)
    b = json.loads(second.output)
    assert a["transcript_hash"] == b["transcript_hash"]
    assert a["verdicts"] == b["verdicts"]


def test_doctor_endpoint_records_audit_chain_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    with stub_endpoint_server() as base_url:
        result = _invoke(["doctor", "--endpoint", base_url, "--endpoint-model", "tiny-coder"])
    assert result.exit_code == 0, result.output

    from bernstein.core.security.audit_chain import (
        EVENT_ENDPOINT_CERTIFICATION,
        AuditChainStore,
    )

    chain = AuditChainStore(tmp_path / ".sdd" / "audit")
    events = chain.query(event_type=EVENT_ENDPOINT_CERTIFICATION)
    assert len(events) == 1
    assert events[0].details["certified_roles"]
