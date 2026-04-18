"""Preflight guard tests — audit-025.

``TaskStore`` is single-process. The server must refuse to boot when
``BERNSTEIN_WORKERS`` / ``WEB_CONCURRENCY`` request more than one uvicorn
worker; these tests pin that contract.
"""

from __future__ import annotations

import pytest

from bernstein.core.server.server_app import (
    _resolve_configured_workers,
    preflight_multi_worker_guard,
)


class TestResolveConfiguredWorkers:
    """Env-var resolution for the preflight guard."""

    def test_defaults_to_one_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_WORKERS", raising=False)
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

        assert _resolve_configured_workers() == 1

    def test_reads_bernstein_workers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_WORKERS", "4")
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

        assert _resolve_configured_workers() == 4

    def test_falls_back_to_web_concurrency(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_WORKERS", raising=False)
        monkeypatch.setenv("WEB_CONCURRENCY", "3")

        assert _resolve_configured_workers() == 3

    def test_bernstein_workers_beats_web_concurrency(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_WORKERS", "2")
        monkeypatch.setenv("WEB_CONCURRENCY", "8")

        assert _resolve_configured_workers() == 2

    def test_ignores_invalid_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_WORKERS", "not-a-number")
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

        assert _resolve_configured_workers() == 1

    def test_ignores_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_WORKERS", "   ")
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

        assert _resolve_configured_workers() == 1

    def test_floor_of_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Zero / negative values resolve to 1, not <1."""
        monkeypatch.setenv("BERNSTEIN_WORKERS", "0")
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

        assert _resolve_configured_workers() == 1


class TestPreflightMultiWorkerGuard:
    """The guard itself — exits on workers>1, passes on single worker."""

    def test_passes_when_single_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_WORKERS", "1")
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

        # No exception — function returns None.
        assert preflight_multi_worker_guard() is None

    def test_passes_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_WORKERS", raising=False)
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

        assert preflight_multi_worker_guard() is None

    def test_raises_on_bernstein_workers_gt_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_WORKERS", "2")
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

        with pytest.raises(SystemExit) as exc:
            preflight_multi_worker_guard()

        message = str(exc.value)
        assert "single-process" in message
        assert "workers=2" in message
        assert "bernstein.yaml" in message
        assert "BERNSTEIN_WORKERS=1" in message

    def test_raises_on_web_concurrency_gt_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_WORKERS", raising=False)
        monkeypatch.setenv("WEB_CONCURRENCY", "8")

        with pytest.raises(SystemExit) as exc:
            preflight_multi_worker_guard()

        assert "workers=8" in str(exc.value)

    def test_create_app_fires_preflight(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """create_app() must call the guard before any work — audit-025."""
        from bernstein.core.server.server_app import create_app

        monkeypatch.setenv("BERNSTEIN_WORKERS", "4")
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

        with pytest.raises(SystemExit) as exc:
            create_app(jsonl_path=tmp_path / "tasks.jsonl")

        assert "workers=4" in str(exc.value)
