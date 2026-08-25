"""Unit tests for the ticket import command and providers."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.ticket_cmd import (
    build_task_payload,
    from_ticket,
    infer_role,
    infer_scope,
)
from bernstein.core.integrations.tickets import (
    TicketAuthError,
    TicketParseError,
    TicketPayload,
    fetch_ticket,
)

# ---------------------------------------------------------------------------
# URL routing
# ---------------------------------------------------------------------------


def _payload(source: str = "github") -> TicketPayload:
    return TicketPayload(
        id="ENG-1",
        title="Example",
        description="body",
        labels=(),
        assignee=None,
        url="https://example.test",
        source=source,  # type: ignore[arg-type]
    )


def test_routes_linear_web_url_to_linear_provider() -> None:
    with patch(
        "bernstein.core.integrations.tickets.linear.fetch_linear",
        return_value=_payload("linear"),
    ) as mock_linear:
        result = fetch_ticket("https://linear.app/acme/issue/ENG-123")
    mock_linear.assert_called_once()
    assert result.source == "linear"


def test_routes_linear_scheme_to_linear_provider() -> None:
    with patch(
        "bernstein.core.integrations.tickets.linear.fetch_linear",
        return_value=_payload("linear"),
    ) as mock_linear:
        fetch_ticket("linear://ENG-42")
    mock_linear.assert_called_once()


def test_routes_github_url_to_github_provider() -> None:
    with patch(
        "bernstein.core.integrations.tickets.github_issues.fetch_github_issue",
        return_value=_payload("github"),
    ) as mock_gh:
        result = fetch_ticket("https://github.com/acme/widgets/issues/7")
    mock_gh.assert_called_once()
    assert result.source == "github"


def test_routes_jira_url_to_jira_provider() -> None:
    with patch(
        "bernstein.core.integrations.tickets.jira.fetch_jira",
        return_value=_payload("jira"),
    ) as mock_jira:
        fetch_ticket("https://acme.atlassian.net/browse/ENG-9")
    mock_jira.assert_called_once()


def test_unrecognized_url_raises_parse_error() -> None:
    with pytest.raises(TicketParseError):
        fetch_ticket("https://example.com/not/a/ticket")


# ---------------------------------------------------------------------------
# Linear auth
# ---------------------------------------------------------------------------


def test_linear_raises_auth_error_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.integrations.tickets import linear

    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(TicketAuthError) as excinfo:
        linear.fetch_linear("https://linear.app/acme/issue/ENG-1")
    assert "LINEAR_API_KEY" in str(excinfo.value)


def test_linear_fetch_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.integrations.tickets import linear

    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")

    response = {
        "data": {
            "issue": {
                "identifier": "ENG-42",
                "title": "Fix login",
                "description": "It is broken.",
                "url": "https://linear.app/acme/issue/ENG-42",
                "labels": {"nodes": [{"name": "bug"}, {"name": "frontend"}]},
                "assignee": {"displayName": "Ada Lovelace", "name": "ada"},
            }
        }
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = response

    with patch("httpx.post", return_value=mock_resp) as mock_post:
        payload = linear.fetch_linear("https://linear.app/acme/issue/ENG-42")

    mock_post.assert_called_once()
    assert payload.id == "ENG-42"
    assert payload.title == "Fix login"
    assert payload.labels == ("bug", "frontend")
    assert payload.assignee == "Ada Lovelace"
    assert payload.source == "linear"


# ---------------------------------------------------------------------------
# GitHub: gh CLI path and REST fallback
# ---------------------------------------------------------------------------


def test_github_gh_cli_path_parses() -> None:
    from bernstein.core.integrations.tickets import github_issues

    gh_stdout = json.dumps(
        {
            "number": 7,
            "title": "Add tests",
            "body": "We need more.",
            "labels": [{"name": "docs"}],
            "assignees": [{"login": "alice"}],
            "url": "https://github.com/acme/widgets/issues/7",
        }
    )

    proc = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=gh_stdout, stderr="")

    with (
        patch.object(github_issues, "_gh_available", return_value=True),
        patch.object(github_issues.subprocess, "run", return_value=proc) as mock_run,
    ):
        payload = github_issues.fetch_github_issue("https://github.com/acme/widgets/issues/7")

    mock_run.assert_called_once()
    assert payload.id == "acme/widgets#7"
    assert payload.title == "Add tests"
    assert payload.labels == ("docs",)
    assert payload.assignee == "alice"
    assert payload.source == "github"


def test_github_rest_fallback_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.integrations.tickets import github_issues

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    rest_body = {
        "number": 11,
        "title": "Refactor",
        "body": "Split the function.",
        "labels": [{"name": "backend"}, "refactor"],
        "assignee": {"login": "bob"},
        "html_url": "https://github.com/acme/widgets/issues/11",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = rest_body

    with (
        patch.object(github_issues, "_gh_available", return_value=False),
        patch("httpx.get", return_value=mock_resp) as mock_get,
    ):
        payload = github_issues.fetch_github_issue("https://github.com/acme/widgets/issues/11")

    mock_get.assert_called_once()
    assert payload.id == "acme/widgets#11"
    assert payload.title == "Refactor"
    assert payload.labels == ("backend", "refactor")
    assert payload.assignee == "bob"


def test_github_rest_missing_token_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.integrations.tickets import github_issues

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with patch.object(github_issues, "_gh_available", return_value=False):
        with pytest.raises(TicketAuthError) as excinfo:
            github_issues.fetch_github_issue("https://github.com/acme/widgets/issues/1")
    assert "GITHUB_TOKEN" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Jira parsing (incl. ADF description)
# ---------------------------------------------------------------------------


def test_jira_parses_adf_description(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.integrations.tickets import jira

    monkeypatch.setenv("JIRA_EMAIL", "user@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    adf_description = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "First paragraph. "},
                    {"type": "text", "text": "More text."},
                ],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Second paragraph."}],
            },
        ],
    }
    body = {
        "key": "ENG-9",
        "fields": {
            "summary": "Improve logging",
            "description": adf_description,
            "labels": ["security", "backend"],
            "assignee": {"displayName": "Carol"},
        },
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = body

    with patch("httpx.get", return_value=mock_resp):
        payload = jira.fetch_jira("https://acme.atlassian.net/browse/ENG-9")

    assert payload.id == "ENG-9"
    assert payload.title == "Improve logging"
    assert "First paragraph" in payload.description
    assert "Second paragraph" in payload.description
    assert payload.labels == ("security", "backend")
    assert payload.assignee == "Carol"
    assert payload.source == "jira"


# ---------------------------------------------------------------------------
# Role / scope inference
# ---------------------------------------------------------------------------


def test_role_inference_bug_to_qa() -> None:
    assert infer_role(("bug",)) == "qa"


def test_role_inference_docs_to_docs() -> None:
    assert infer_role(("documentation",)) == "docs"
    assert infer_role(("docs",)) == "docs"


def test_role_inference_fallback_to_default() -> None:
    assert infer_role(("weird", "unseen")) == "backend"
    assert infer_role((), default="custom") == "custom"


def test_scope_inference() -> None:
    assert infer_scope(("small",)) == "small"
    assert infer_scope(("epic",)) == "large"
    assert infer_scope(()) == "medium"


# ---------------------------------------------------------------------------
# CLI: dry-run, metadata, --run
# ---------------------------------------------------------------------------


def _fake_ticket(**overrides: Any) -> TicketPayload:
    defaults: dict[str, Any] = {
        "id": "ENG-1",
        "title": "Fix things",
        "description": "Lots to fix.",
        "labels": ("bug",),
        "assignee": "alice",
        "url": "https://linear.app/acme/issue/ENG-1",
        "source": "linear",
    }
    defaults.update(overrides)
    return TicketPayload(**defaults)


def test_dry_run_does_not_call_task_store() -> None:
    runner = CliRunner()
    with (
        patch("bernstein.cli.commands.ticket_cmd.fetch_ticket", return_value=_fake_ticket()),
        patch("bernstein.cli.commands.ticket_cmd.server_post") as mock_post,
    ):
        result = runner.invoke(
            from_ticket,
            ["https://linear.app/acme/issue/ENG-1", "--dry-run"],
        )
    assert result.exit_code == 0, result.output
    mock_post.assert_not_called()
    # Dry-run should emit the payload JSON
    assert "metadata" in result.output
    assert "ENG-1" in result.output


def test_task_payload_carries_source_and_external_id() -> None:
    ticket = _fake_ticket(source="jira", id="ENG-42", labels=("docs",))
    payload = build_task_payload(ticket, role=None, priority=None)
    assert payload["metadata"] == {
        "source": "jira",
        "external_id": "ENG-42",
        "url": "https://linear.app/acme/issue/ENG-1",
    }
    # Role inferred from label
    assert payload["role"] == "docs"


def test_create_path_posts_to_server_and_honors_run_flag() -> None:
    runner = CliRunner()
    fake_post = MagicMock(return_value={"id": "tsk_abc"})
    with (
        patch("bernstein.cli.commands.ticket_cmd.fetch_ticket", return_value=_fake_ticket()),
        patch("bernstein.cli.commands.ticket_cmd.server_post", fake_post),
        patch("bernstein.cli.commands.ticket_cmd.subprocess.call", return_value=0) as mock_call,
    ):
        result = runner.invoke(
            from_ticket,
            ["https://linear.app/acme/issue/ENG-1", "--run", "--priority", "high"],
        )

    assert result.exit_code == 0, result.output
    fake_post.assert_called_once()
    posted_path, posted_payload = fake_post.call_args.args
    assert posted_path == "/tasks"
    # --priority high maps to 1
    assert posted_payload["priority"] == 1
    mock_call.assert_called_once()
    assert mock_call.call_args.args[0][-1] == "tsk_abc"


def test_explicit_role_flag_overrides_inference() -> None:
    ticket = _fake_ticket(labels=("bug",))
    payload = build_task_payload(ticket, role="backend", priority="low")
    assert payload["role"] == "backend"
    assert payload["priority"] == 3


# ---------------------------------------------------------------------------
# HTTP helper: retries, rate limits, and circuit breaker (Issue #4534)
# ---------------------------------------------------------------------------


def test_429_with_retry_after_is_retried_and_succeeds() -> None:
    from bernstein.core.integrations.tickets._http import http_get_json
    from bernstein.core.observability.provider_circuit_breaker import ProviderCircuitBreaker

    calls = 0
    slept: list[float] = []

    class FakeResponse:
        def __init__(self, status_code: int, headers: dict[str, str], json_data: dict[str, Any] | None = None):
            self.status_code = status_code
            self.headers = headers
            self._json = json_data or {}
            self.text = "rate limited"

        def json(self) -> dict[str, Any]:
            return self._json

    def mock_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeResponse(429, {"Retry-After": "2.5"})
        return FakeResponse(200, {}, {"title": "Issue title"})

    breaker = ProviderCircuitBreaker("test-provider")

    with patch("httpx.get", side_effect=mock_get):
        data = http_get_json(
            url="https://api.github.com/repos/acme/repo/issues/1",
            headers={"Authorization": "Bearer tok"},
            provider_label="GitHub",
            auth_env_var="GITHUB_TOKEN",
            circuit_breaker=breaker,
            sleep_fn=slept.append,
        )

    assert calls == 2
    assert slept == [2.5]
    assert data["title"] == "Issue title"
    assert breaker.state.value == "closed"


def test_a_huge_retry_after_is_capped_not_obeyed() -> None:
    """A provider must not be able to park the calling thread.

    ``Retry-After`` is a server-controlled number and this helper sleeps on it
    synchronously, so honouring it unbounded hands any tracker the ability to
    block a ticket sync for as long as it likes. The existing coverage uses
    2.5s, which is under the cap and so passes either way; this pins the cap.
    """
    from bernstein.core.integrations.tickets._http import http_get_json
    from bernstein.core.observability.provider_circuit_breaker import ProviderCircuitBreaker

    slept: list[float] = []
    calls = 0

    class FakeResponse:
        def __init__(self, status_code: int, headers: dict[str, str], json_data: dict[str, Any] | None = None):
            self.status_code = status_code
            self.headers = headers
            self._json = json_data or {}
            self.text = "rate limited"

        def json(self) -> dict[str, Any]:
            return self._json

    def mock_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeResponse(429, {"Retry-After": "3600"})
        return FakeResponse(200, {}, {"title": "ok"})

    breaker = ProviderCircuitBreaker("cap-httpx")
    with patch("httpx.get", side_effect=mock_get):
        http_get_json(
            url="https://api.github.com/repos/acme/repo/issues/1",
            headers={"Authorization": "Bearer tok"},
            provider_label="GitHub",
            auth_env_var="GITHUB_TOKEN",
            circuit_breaker=breaker,
            max_backoff_s=30.0,
            sleep_fn=slept.append,
        )

    assert slept == [30.0], f"Retry-After must be capped at max_backoff_s; slept {slept}"


def test_the_urllib_fallback_caps_retry_after_the_same_way() -> None:
    """The two transports must not disagree about how long they will wait.

    ``http_request_json`` falls back to ``urllib`` when ``httpx`` is absent, and
    that branch is otherwise uncovered. A response that costs 30s through one
    transport must not cost an hour through the other.
    """
    import sys
    import urllib.error

    from bernstein.core.integrations.tickets._http import http_get_json
    from bernstein.core.observability.provider_circuit_breaker import ProviderCircuitBreaker

    slept: list[float] = []
    calls = 0

    class _Headers(dict[str, str]):
        pass

    def mock_urlopen(req: Any, timeout: float | None = None) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                "https://api.github.com/x", 429, "Too Many Requests", _Headers({"Retry-After": "3600"}), None
            )

        class _Handle:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"title": "ok"}'

        return _Handle()

    breaker = ProviderCircuitBreaker("cap-urllib")
    with (
        patch.dict(sys.modules, {"httpx": None}),
        patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        http_get_json(
            url="https://api.github.com/repos/acme/repo/issues/1",
            headers={"Authorization": "Bearer tok"},
            provider_label="GitHub",
            auth_env_var="GITHUB_TOKEN",
            circuit_breaker=breaker,
            max_backoff_s=30.0,
            sleep_fn=slept.append,
        )

    assert calls == 2
    assert slept == [30.0], f"urllib fallback must cap Retry-After too; slept {slept}"


def test_404_fails_immediately_without_retry() -> None:
    from bernstein.core.integrations.tickets._http import http_get_json
    from bernstein.core.observability.provider_circuit_breaker import ProviderCircuitBreaker

    slept: list[float] = []

    class FakeResponse:
        status_code = 404
        headers: dict[str, str] = {}
        text = "Not Found"

        def json(self) -> dict[str, Any]:
            return {}

    breaker = ProviderCircuitBreaker("test-provider")

    with patch("httpx.get", return_value=FakeResponse()):
        with pytest.raises(TicketParseError) as exc_info:
            http_get_json(
                url="https://api.github.com/repos/acme/repo/issues/999",
                headers={},
                provider_label="GitHub",
                auth_env_var="GITHUB_TOKEN",
                circuit_breaker=breaker,
                sleep_fn=slept.append,
            )

    assert "404" in str(exc_info.value)
    assert len(slept) == 0


def test_retry_exhaustion_raises_rate_limited_error_not_parse_error() -> None:
    from bernstein.core.integrations.tickets import TicketRateLimitError
    from bernstein.core.integrations.tickets._http import http_get_json
    from bernstein.core.observability.provider_circuit_breaker import ProviderCircuitBreaker

    calls = 0
    slept: list[float] = []

    class FakeResponse:
        status_code = 429
        headers = {"Retry-After": "1"}
        text = "Too Many Requests"

        def json(self) -> dict[str, Any]:
            return {}

    def mock_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse()

    breaker = ProviderCircuitBreaker("test-provider")

    with patch("httpx.get", side_effect=mock_get):
        with pytest.raises(TicketRateLimitError) as exc_info:
            http_get_json(
                url="https://api.github.com/repos/acme/repo/issues/1",
                headers={},
                provider_label="GitHub",
                auth_env_var="GITHUB_TOKEN",
                max_retries=2,
                circuit_breaker=breaker,
                sleep_fn=slept.append,
            )

    assert calls == 3  # initial + 2 retries
    assert len(slept) == 2
    assert "rate limit exceeded" in str(exc_info.value)
    assert exc_info.value.provider == "GitHub"


def test_repeated_provider_failures_open_the_circuit() -> None:
    from bernstein.core.integrations.tickets import TicketCircuitOpenError
    from bernstein.core.integrations.tickets._http import http_get_json
    from bernstein.core.observability.provider_circuit_breaker import CircuitBreakerConfig, ProviderCircuitBreaker

    class FakeResponse:
        status_code = 500
        headers: dict[str, str] = {}
        text = "Internal Server Error"

        def json(self) -> dict[str, Any]:
            return {}

    config = CircuitBreakerConfig(failure_threshold=2, recovery_timeout_s=60.0)
    breaker = ProviderCircuitBreaker("jira", config=config)

    with patch("httpx.get", return_value=FakeResponse()):
        # Attempt 1 -> Failure
        with pytest.raises(TicketParseError):
            http_get_json(
                url="https://jira.example.com/rest/api/2/issue/1",
                headers={},
                provider_label="Jira",
                auth_env_var="JIRA_API_TOKEN",
                circuit_breaker=breaker,
            )
        assert breaker.state.value == "closed"

        # Attempt 2 -> Failure (hits threshold 2) -> Transitions to OPEN
        with pytest.raises(TicketParseError):
            http_get_json(
                url="https://jira.example.com/rest/api/2/issue/1",
                headers={},
                provider_label="Jira",
                auth_env_var="JIRA_API_TOKEN",
                circuit_breaker=breaker,
            )
        assert breaker.state.value == "open"

        # Attempt 3 -> Immediately rejected by open circuit without HTTP call
        with pytest.raises(TicketCircuitOpenError) as exc_info:
            http_get_json(
                url="https://jira.example.com/rest/api/2/issue/1",
                headers={},
                provider_label="Jira",
                auth_env_var="JIRA_API_TOKEN",
                circuit_breaker=breaker,
            )
        assert "circuit breaker is OPEN" in str(exc_info.value)
