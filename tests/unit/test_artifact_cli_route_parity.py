"""The CLI and the server cannot disagree about an artifact (#2559, AC1).

The determinism criterion is not "two implementations were checked against each
other once". It is structural: ``bernstein artifact health --json`` and
``GET /artifacts/health`` are two callers of one function, and one function
serialises the verdict. These tests pin the observable consequence -- the same
``.sdd`` state and the same evaluation instant give byte-identical bytes on both
surfaces -- and the attribution surface (AC5) that reads off the same chain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.cli.commands.artifact_cmd import artifact_group
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.routes.artifacts import router as artifacts_router

_KEY = b"parity-key"
_URI = "pkg://bernstein/3.9.0"
_PKG_URI = "pkg://pypi/bernstein/3.9.0"
_PR_URI = "pr://github.com/acme/widget/2559"


@pytest.fixture(autouse=True)
def _pin_hmac_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both surfaces resolve the spine key the same way; pin it for the test."""
    monkeypatch.setattr("bernstein.cli.commands.artifact_cmd._spine_hmac_key", lambda: _KEY)
    monkeypatch.setattr("bernstein.core.routes.artifacts._hmac_key", lambda: _KEY)


def _record(
    workdir: Path,
    uri: str,
    content: bytes,
    *,
    ts: int,
    run_id: str = "run-1",
    actor: str = "agent-release",
    model: str = "claude-opus-5",
) -> str:
    return LineageSpine(workdir / ".sdd" / "lineage", run_id=run_id, hmac_key=_KEY).record(
        artifact_path=uri,
        content=content,
        actor=actor,
        step_id="publish",
        model=model,
        timestamp=ts,
    )


def _client(workdir: Path) -> TestClient:
    app = FastAPI()
    app.include_router(artifacts_router)
    app.state.workdir = workdir
    return TestClient(app)


def _cli(args: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(artifact_group, args)
    return result.exit_code, result.output


# ---------------------------------------------------------------------------
# Byte-identical verdicts (AC1)
# ---------------------------------------------------------------------------


def test_cli_and_route_verdicts_are_byte_identical(tmp_path: Path) -> None:
    _record(tmp_path, _PKG_URI, b"wheel", ts=100)

    code, cli_out = _cli(["health", _PKG_URI, "-w", str(tmp_path), "--at", "500", "--output-json"])
    assert code == 0
    route_out = _client(tmp_path).get("/artifacts/health", params={"uri": _PKG_URI, "at": 500}).text

    assert cli_out.strip() == route_out
    assert json.loads(route_out)["verdict"] == "green"


def test_byte_identity_survives_a_red_verdict(tmp_path: Path) -> None:
    _record(tmp_path, _PKG_URI, b"wheel", ts=100)
    spine_path = tmp_path / ".sdd" / "lineage" / "run-1" / "spine.jsonl"
    row = json.loads(spine_path.read_bytes().strip())
    row["actor"] = "impostor"
    spine_path.write_bytes(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n")

    code, cli_out = _cli(["health", _PKG_URI, "-w", str(tmp_path), "--at", "500", "--output-json"])
    route_out = _client(tmp_path).get("/artifacts/health", params={"uri": _PKG_URI, "at": 500}).text

    assert cli_out.strip() == route_out
    assert json.loads(route_out)["verdict"] == "red"
    # A red artifact is a non-zero exit for the CLI and a successfully served
    # answer for the route: the verdict travels in the payload, not the status.
    assert code == 2


def test_byte_identity_survives_an_amber_verdict(tmp_path: Path) -> None:
    _record(tmp_path, _PKG_URI, b"wheel", ts=100)
    code, cli_out = _cli(
        ["health", _PKG_URI, "-w", str(tmp_path), "--at", "100000", "--cadence-seconds", "10", "--output-json"]
    )
    route_out = (
        _client(tmp_path).get("/artifacts/health", params={"uri": _PKG_URI, "at": 100000, "cadence_seconds": 10}).text
    )
    assert cli_out.strip() == route_out
    assert json.loads(route_out)["verdict"] == "amber"
    assert code == 0


def test_byte_identity_for_a_never_produced_artifact(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir()
    _, cli_out = _cli(["health", _PR_URI, "-w", str(tmp_path), "--at", "500", "--output-json"])
    route_out = _client(tmp_path).get("/artifacts/health", params={"uri": _PR_URI, "at": 500}).text
    assert cli_out.strip() == route_out


@pytest.mark.parametrize(
    "uri",
    [_PKG_URI, _PR_URI, "deploy://prod/docs-site", "doc://example.test/lineage", "src/bernstein/core/lineage/spine.py"],
)
def test_byte_identity_across_every_scheme(tmp_path: Path, uri: str) -> None:
    _record(tmp_path, uri, b"bytes", ts=100)
    _, cli_out = _cli(["health", uri, "-w", str(tmp_path), "--at", "500", "--output-json"])
    route_out = _client(tmp_path).get("/artifacts/health", params={"uri": uri, "at": 500}).text
    assert cli_out.strip() == route_out


def test_the_route_serves_canonical_json_not_reordered_json(tmp_path: Path) -> None:
    """FastAPI's encoder must not get a chance to re-order the verdict keys."""
    _record(tmp_path, _PKG_URI, b"wheel", ts=100)
    body = _client(tmp_path).get("/artifacts/health", params={"uri": _PKG_URI, "at": 500}).text
    assert body == json.dumps(json.loads(body), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def test_repeated_route_calls_are_byte_identical(tmp_path: Path) -> None:
    _record(tmp_path, _PKG_URI, b"wheel", ts=100)
    client = _client(tmp_path)
    first = client.get("/artifacts/health", params={"uri": _PKG_URI, "at": 500}).text
    second = client.get("/artifacts/health", params={"uri": _PKG_URI, "at": 500}).text
    assert first == second


# ---------------------------------------------------------------------------
# Attribution (AC5)
# ---------------------------------------------------------------------------


def test_log_names_the_identity_and_model_behind_the_current_tip(tmp_path: Path) -> None:
    _record(tmp_path, _PR_URI, b"head-1", ts=100, actor="agent-old", model="model-old")
    newest = _record(tmp_path, _PR_URI, b"head-2", ts=200, actor="agent-new", model="model-new")

    _, cli_out = _cli(["log", _PR_URI, "-w", str(tmp_path), "--output-json"])
    payload = json.loads(cli_out)
    assert payload["uri"] == _PR_URI
    tip = payload["productions"][0]
    assert tip["entry_hash"] == newest
    assert tip["actor"] == "agent-new"
    assert tip["model"] == "model-new"
    assert tip["verified"] is True


def test_log_is_identical_on_both_surfaces(tmp_path: Path) -> None:
    _record(tmp_path, _PKG_URI, b"wheel", ts=100)
    _, cli_out = _cli(["log", _PKG_URI, "-w", str(tmp_path), "--output-json"])
    route_out = _client(tmp_path).get("/artifacts/log", params={"uri": _PKG_URI}).text
    assert cli_out.strip() == route_out


def test_log_human_output_flags_a_tampered_production(tmp_path: Path) -> None:
    _record(tmp_path, _PKG_URI, b"wheel", ts=100)
    spine_path = tmp_path / ".sdd" / "lineage" / "run-1" / "spine.jsonl"
    row = json.loads(spine_path.read_bytes().strip())
    row["model"] = "a-model-nobody-ran"
    spine_path.write_bytes(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n")

    _, out = _cli(["log", _PKG_URI, "-w", str(tmp_path)])
    assert "TAMPERED" in out


def test_log_limit_is_honoured_on_both_surfaces(tmp_path: Path) -> None:
    for i in range(5):
        _record(tmp_path, _PKG_URI, f"v{i}".encode(), ts=i)
    _, cli_out = _cli(["log", _PKG_URI, "-w", str(tmp_path), "--limit", "2", "--output-json"])
    route_out = _client(tmp_path).get("/artifacts/log", params={"uri": _PKG_URI, "limit": 2}).text
    assert cli_out.strip() == route_out
    assert len(json.loads(route_out)["productions"]) == 2


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_matches_on_both_surfaces(tmp_path: Path) -> None:
    _record(tmp_path, _PKG_URI, b"wheel", ts=100)
    _record(tmp_path, "docs/report.md", b"doc", ts=200)

    _, cli_out = _cli(["list", "-w", str(tmp_path), "--output-json"])
    route_payload = _client(tmp_path).get("/artifacts").json()
    assert json.loads(cli_out)["artifacts"] == [
        {"productions": n["productions"], "uri": n["uri"]} for n in route_payload["artifacts"]
    ]


def test_list_human_output_is_empty_on_a_fresh_project(tmp_path: Path) -> None:
    code, out = _cli(["list", "-w", str(tmp_path)])
    assert code == 0
    assert "no artifacts recorded" in out


# ---------------------------------------------------------------------------
# Route input handling
# ---------------------------------------------------------------------------


def test_the_route_requires_a_uri(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir()
    assert _client(tmp_path).get("/artifacts/health").status_code == 400


def test_the_route_rejects_a_non_integer_instant(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir()
    resp = _client(tmp_path).get("/artifacts/health", params={"uri": _PKG_URI, "at": "yesterday"})
    assert resp.status_code == 400


def test_the_artifact_routes_are_mounted_on_the_real_app(tmp_path: Path) -> None:
    """A route nobody mounted cannot disagree with the CLI, but it also cannot serve."""
    from bernstein.core.server import create_app

    application = create_app(jsonl_path=tmp_path / "runtime" / "tasks.jsonl")
    paths = {route.path for route in application.routes}  # type: ignore[attr-defined]
    assert {"/artifacts", "/artifacts/health", "/artifacts/log"} <= paths
    # AUDIT-126: every router is mounted under the versioned surface too.
    assert {"/api/v1/artifacts", "/api/v1/artifacts/health", "/api/v1/artifacts/log"} <= paths


@pytest.mark.parametrize(
    "hostile",
    ["../etc/passwd", "/etc/passwd", "a/../../etc/passwd", "ftp://evil.test/x", "PKG://PyPI/x/1.0"],
)
def test_a_hostile_key_resolves_to_nothing_on_both_surfaces(tmp_path: Path, hostile: str) -> None:
    """Path-safety verdicts are unchanged; an unwritable key simply has no history."""
    _record(tmp_path, _PKG_URI, b"wheel", ts=100)
    _, cli_out = _cli(["health", hostile, "-w", str(tmp_path), "--at", "500", "--output-json"])
    route_out = _client(tmp_path).get("/artifacts/health", params={"uri": hostile, "at": 500}).text
    assert cli_out.strip() == route_out
    assert json.loads(route_out)["verdict"] == "red"
    assert json.loads(route_out)["production_count"] == 0
