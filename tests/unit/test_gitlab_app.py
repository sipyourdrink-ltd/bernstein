"""Unit tests for ``bernstein.gitlab_app.app`` - config + URL helpers."""

from __future__ import annotations

import pytest

from bernstein.gitlab_app.app import (
    DEFAULT_GITLAB_URL,
    GitLabAppConfig,
    build_api_url,
    build_auth_headers,
    fetch_job_trace,
    get_gitlab_base_url,
)


class TestGetGitlabBaseUrl:
    """``get_gitlab_base_url`` reads BERNSTEIN_GITLAB_URL with sane defaults."""

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_GITLAB_URL", raising=False)
        assert get_gitlab_base_url() == DEFAULT_GITLAB_URL

    def test_https_url_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_GITLAB_URL", "https://gitlab.example.com")
        assert get_gitlab_base_url() == "https://gitlab.example.com"

    def test_trailing_slash_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_GITLAB_URL", "https://gitlab.example.com/")
        assert get_gitlab_base_url() == "https://gitlab.example.com"

    def test_http_localhost_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_GITLAB_URL", "http://localhost:8080")
        assert get_gitlab_base_url() == "http://localhost:8080"

    def test_invalid_scheme_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_GITLAB_URL", "ftp://gitlab.example.com")
        assert get_gitlab_base_url() == DEFAULT_GITLAB_URL

    def test_garbage_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_GITLAB_URL", "not-a-url")
        assert get_gitlab_base_url() == DEFAULT_GITLAB_URL

    def test_empty_string_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_GITLAB_URL", "   ")
        assert get_gitlab_base_url() == DEFAULT_GITLAB_URL


class TestGitLabAppConfig:
    """``GitLabAppConfig.from_env`` validation."""

    def test_from_env_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_GITLAB_URL", "https://gitlab.example.com")
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        monkeypatch.setenv("GITLAB_WEBHOOK_TOKEN", "secret-hook")
        cfg = GitLabAppConfig.from_env()
        assert cfg.base_url == "https://gitlab.example.com"
        assert cfg.token == "glpat-test"
        assert cfg.webhook_token == "secret-hook"

    def test_from_env_pat_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.setenv("GITLAB_PAT", "glpat-pat")
        monkeypatch.setenv("GITLAB_WEBHOOK_TOKEN", "hook")
        cfg = GitLabAppConfig.from_env()
        assert cfg.token == "glpat-pat"

    def test_from_env_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("GITLAB_PAT", raising=False)
        monkeypatch.setenv("GITLAB_WEBHOOK_TOKEN", "hook")
        with pytest.raises(ValueError, match="GITLAB_TOKEN"):
            GitLabAppConfig.from_env()

    def test_from_env_missing_hook_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "x")
        monkeypatch.delenv("GITLAB_WEBHOOK_TOKEN", raising=False)
        with pytest.raises(ValueError, match="GITLAB_WEBHOOK_TOKEN"):
            GitLabAppConfig.from_env()

    def test_config_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        cfg = GitLabAppConfig(base_url="https://gitlab.com", token="t", webhook_token="w")
        with pytest.raises(FrozenInstanceError):
            cfg.token = "other"  # type: ignore[misc]


class TestBuildApiUrl:
    """``build_api_url`` path handling."""

    def test_simple_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_GITLAB_URL", raising=False)
        assert build_api_url("/projects/1") == "https://gitlab.com/api/v4/projects/1"

    def test_missing_leading_slash_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_GITLAB_URL", raising=False)
        assert build_api_url("projects/1") == "https://gitlab.com/api/v4/projects/1"

    def test_custom_base(self) -> None:
        assert build_api_url("/projects/1", "https://gitlab.acme.com") == "https://gitlab.acme.com/api/v4/projects/1"

    def test_base_trailing_slash(self) -> None:
        assert build_api_url("/x", "https://gitlab.acme.com/") == "https://gitlab.acme.com/api/v4/x"


class TestBuildAuthHeaders:
    def test_token_set(self) -> None:
        assert build_auth_headers("abc") == {"PRIVATE-TOKEN": "abc"}

    def test_empty_token(self) -> None:
        assert build_auth_headers("") == {}


class TestFetchJobTrace:
    """``fetch_job_trace`` graceful degradation."""

    def test_no_token_returns_empty(self) -> None:
        assert fetch_job_trace(1, 2, token="") == ""

    def test_httpx_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Response:
            status_code = 200
            text = "trace body"

        class _FakeHttpx:
            @staticmethod
            def get(url: str, headers: dict[str, str], timeout: float) -> _Response:
                assert "api/v4/projects/1/jobs/2/trace" in url
                assert headers["PRIVATE-TOKEN"] == "t"
                return _Response()

        import sys

        monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx)
        assert fetch_job_trace(1, 2, token="t") == "trace body"

    def test_httpx_non_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Response:
            status_code = 404
            text = ""

        class _FakeHttpx:
            @staticmethod
            def get(url: str, headers: dict[str, str], timeout: float) -> _Response:
                return _Response()

        import sys

        monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx)
        assert fetch_job_trace(1, 2, token="t") == ""

    def test_httpx_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FakeHttpx:
            @staticmethod
            def get(*_args: object, **_kwargs: object) -> object:
                raise RuntimeError("boom")

        import sys

        monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx)
        assert fetch_job_trace(1, 2, token="t") == ""
