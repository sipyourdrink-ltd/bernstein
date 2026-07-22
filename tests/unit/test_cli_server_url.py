from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.cli import helpers
from bernstein.core.defaults import SDD_SERVER_PORT


def test_resolve_server_url_uses_persisted_workspace_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
    helpers.persist_server_port(8062, tmp_path)

    assert helpers.resolve_server_url(tmp_path) == "http://127.0.0.1:8062"


def test_resolve_server_url_env_overrides_persisted_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helpers.persist_server_port(8062, tmp_path)
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", "https://bernstein.example/")

    assert helpers.resolve_server_url(tmp_path) == "https://bernstein.example"


def test_resolve_server_url_ignores_invalid_runtime_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
    port_path = tmp_path / SDD_SERVER_PORT
    port_path.parent.mkdir(parents=True)
    port_path.write_text("not-a-port\n")

    assert helpers.resolve_server_url(tmp_path) == "http://127.0.0.1:8052"


def test_server_get_targets_the_persisted_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
    helpers.persist_server_port(8062)
    requested: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, bool]:
            return {"ok": True}

    def fake_get(url: str, **kwargs: object) -> Response:
        requested.append(url)
        return Response()

    with patch.object(helpers.httpx, "get", fake_get):
        assert helpers.server_get("/status") == {"ok": True}
    assert requested == ["http://127.0.0.1:8062/status"]


def test_server_post_targets_the_persisted_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "test-token")
    helpers.persist_server_port(8062)
    calls: list[tuple[str, dict[str, object]]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, bool]:
            return {"ok": True}

    def fake_post(url: str, **kwargs: object) -> Response:
        calls.append((url, kwargs))
        return Response()

    with patch.object(helpers.httpx, "post", fake_post):
        assert helpers.server_post("/tasks", {"name": "demo"}) == {"ok": True}
    assert calls == [
        (
            "http://127.0.0.1:8062/tasks",
            {
                "json": {"name": "demo"},
                "timeout": 5.0,
                "headers": {"Authorization": "Bearer test-token"},
            },
        )
    ]
