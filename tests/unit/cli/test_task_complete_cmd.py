"""Tests for ``bernstein task complete`` (issue #3015).

The completion path used to be expressed as a raw ``curl`` the agent had to
hand-assemble - nested JSON quotes, a shell ``$(cat …token)`` substitution and
a Bearer header, all inside one ``run_command`` string. Smaller models drowned
in the quoting and never emitted the call.

``bernstein task complete <task_id> --summary "…"`` replaces that with a
first-class affordance: the CLI resolves the server URL and the session token
itself (env var or the persisted run-token file) and POSTs the completion. The
agent never constructs an auth header or a JSON body.

These tests exercise the full ``server_post`` → ``auth_headers`` →
``resolve_server_url`` chain and only mock the network boundary, so the
token-reading behaviour is asserted end to end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import bernstein.cli.helpers as helpers
from bernstein.cli.commands.task_cmd import task_group
from bernstein.core.run_auth_token import persist_run_auth_token


class _FakeResponse:
    """Minimal stand-in for an ``httpx.Response`` returned by the task server."""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        import httpx

        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("POST", "http://127.0.0.1:8052/x"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _Capture:
    """Records the arguments the CLI hands to ``httpx.post``."""

    def __init__(self) -> None:
        self.url: str | None = None
        self.json: dict[str, Any] | None = None
        self.headers: dict[str, str] | None = None


def _patch_post(
    monkeypatch: pytest.MonkeyPatch,
    capture: _Capture,
    *,
    response: _FakeResponse | None = None,
    exc: Exception | None = None,
) -> None:
    def _fake_post(url: str, *, json: Any = None, timeout: float = 0.0, headers: Any = None) -> _FakeResponse:
        capture.url = url
        capture.json = json
        capture.headers = dict(headers or {})
        if exc is not None:
            raise exc
        return response or _FakeResponse({"id": "T-abc", "title": "demo", "status": "done"})

    monkeypatch.setattr(helpers.httpx, "post", _fake_post)


def _workspace_with_token(tmp_path: Path, token: str) -> Path:
    """Create a workdir carrying a persisted run-token file and a server port."""
    workdir = tmp_path / "project"
    workdir.mkdir()
    persist_run_auth_token(workdir, token)
    (workdir / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)
    (workdir / ".sdd" / "runtime" / "server.port").write_text("8052\n")
    return workdir


class TestTaskCompleteCommand:
    def test_reads_token_from_run_file_and_posts_summary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: CLI reads the persisted token itself and POSTs the summary.

        No ``BERNSTEIN_AUTH_TOKEN`` in the environment - the CLI must fall
        back to the ``.sdd/runtime/auth.token`` file, exactly the channel an
        agent has. The captured request proves the URL, the JSON body and the
        Authorization header are all built by the command, not the caller.
        """
        monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
        workdir = _workspace_with_token(tmp_path, "run-token-xyz")
        monkeypatch.chdir(workdir)

        capture = _Capture()
        _patch_post(monkeypatch, capture)

        result = CliRunner().invoke(task_group, ["complete", "T-abc", "--summary", "did the thing"])

        assert result.exit_code == 0, result.output
        assert capture.url is not None and capture.url.endswith("/tasks/T-abc/complete")
        assert capture.json == {"result_summary": "did the thing"}
        assert capture.headers is not None
        assert capture.headers.get("Authorization") == "Bearer run-token-xyz"

    def test_reads_token_from_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ``BERNSTEIN_AUTH_TOKEN`` env channel (agent's exported token) works too."""
        monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "env-token-123")
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
        monkeypatch.chdir(tmp_path)

        capture = _Capture()
        _patch_post(monkeypatch, capture)

        result = CliRunner().invoke(task_group, ["complete", "T-1", "-s", "done"])

        assert result.exit_code == 0, result.output
        assert capture.headers is not None
        assert capture.headers.get("Authorization") == "Bearer env-token-123"

    def test_honours_server_url_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The command targets the configured server URL, not a hard-coded port."""
        monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "t")
        monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://central:9099")
        monkeypatch.chdir(tmp_path)

        capture = _Capture()
        _patch_post(monkeypatch, capture)

        result = CliRunner().invoke(task_group, ["complete", "T-9", "-s", "done"])

        assert result.exit_code == 0, result.output
        assert capture.url == "http://central:9099/tasks/T-9/complete"

    def test_summary_is_required(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A completion with no summary is rejected before any network call."""
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(task_group, ["complete", "T-abc"])
        assert result.exit_code != 0

    def test_unreachable_server_exits_nonzero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A persistent connection error surfaces as a non-zero exit after retries."""
        import httpx

        monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "t")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(helpers.time, "sleep", lambda _s: None)  # no real backoff sleeps

        calls = {"n": 0}

        def _always_refused(url: str, *, json: Any = None, timeout: float = 0.0, headers: Any = None) -> _FakeResponse:
            calls["n"] += 1
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(helpers.httpx, "post", _always_refused)

        result = CliRunner().invoke(task_group, ["complete", "T-abc", "-s", "done"])
        assert result.exit_code == 1
        # 1 initial attempt + 3 connect retries (evolve-mode hot-reload window).
        assert calls["n"] == 4

    def test_retries_on_connect_error_then_succeeds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A transient connrefused (server hot-reload) is retried, not failed."""
        import httpx

        monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "t")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(helpers.time, "sleep", lambda _s: None)

        calls = {"n": 0}

        def _refuse_twice(url: str, *, json: Any = None, timeout: float = 0.0, headers: Any = None) -> _FakeResponse:
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("server restarting")
            return _FakeResponse({"id": "T-abc", "title": "demo", "status": "done"})

        monkeypatch.setattr(helpers.httpx, "post", _refuse_twice)

        result = CliRunner().invoke(task_group, ["complete", "T-abc", "-s", "done"])
        assert result.exit_code == 0, result.output
        assert calls["n"] == 3

    def test_json_output_prints_task_payload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--json`` prints the server's task payload for scripting."""
        monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "t")
        monkeypatch.chdir(tmp_path)
        capture = _Capture()
        _patch_post(
            monkeypatch,
            capture,
            response=_FakeResponse({"id": "T-abc", "title": "demo", "status": "done"}),
        )

        result = CliRunner().invoke(task_group, ["complete", "T-abc", "-s", "done", "--json"])
        assert result.exit_code == 0, result.output
        assert "T-abc" in result.output
        assert "done" in result.output
