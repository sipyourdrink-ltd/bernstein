"""Tests for bernstein.cli.error_suggestions -- CLI-001."""

from __future__ import annotations

from bernstein.cli.error_suggestions import (
    ErrorSuggestion,
    all_suggestions,
    format_suggestion,
    suggest,
    suggest_and_format,
)


class TestSuggest:
    """Test error pattern matching."""

    def test_port_in_use(self) -> None:
        result = suggest("Port 8052 is already in use")
        assert result is not None
        assert "Port already in use" in result.title

    def test_port_eaddrinuse(self) -> None:
        result = suggest("OSError: [Errno 98] Address already in use")
        assert result is not None
        assert "Port" in result.title

    def test_connection_refused(self) -> None:
        result = suggest("httpx.ConnectError: connection refused")
        assert result is not None
        assert "unreachable" in result.title.lower() or "server" in result.title.lower()

    def test_missing_anthropic_key(self) -> None:
        result = suggest("ANTHROPIC_API_KEY is not set")
        assert result is not None
        assert "Claude" in result.title or "API key" in result.title

    def test_missing_openai_key(self) -> None:
        result = suggest("OPENAI_API_KEY is not set")
        assert result is not None
        assert "OpenAI" in result.title or "API key" in result.title

    def test_missing_google_key(self) -> None:
        result = suggest("GOOGLE_API_KEY is not set")
        assert result is not None
        assert "Google" in result.title or "API key" in result.title

    def test_no_cli_agent(self) -> None:
        result = suggest("No supported CLI agent found in PATH")
        assert result is not None
        assert "agent" in result.title.lower()

    def test_no_seed_file(self) -> None:
        result = suggest("No bernstein.yaml found")
        assert result is not None
        assert "configuration" in result.title.lower() or "seed" in result.title.lower()

    def test_budget_exceeded(self) -> None:
        result = suggest("Budget exhausted: spending cap reached")
        assert result is not None
        assert "budget" in result.title.lower()

    def test_rate_limit(self) -> None:
        result = suggest("429 Too Many Requests")
        assert result is not None
        assert "rate limit" in result.title.lower()

    def test_yaml_parse_error(self) -> None:
        result = suggest("yaml.scanner.ScannerError: invalid yaml syntax")
        assert result is not None
        assert "YAML" in result.title

    def test_timeout(self) -> None:
        result = suggest("Operation timed out after 30s")
        assert result is not None
        assert "timed out" in result.title.lower() or "timeout" in result.title.lower()

    def test_disk_full(self) -> None:
        result = suggest("OSError: [Errno 28] No space left on device")
        assert result is not None
        assert "disk" in result.title.lower()

    def test_import_error(self) -> None:
        result = suggest("ModuleNotFoundError: No module named 'bernstein'")
        assert result is not None
        assert "dependency" in result.title.lower() or "Missing" in result.title

    def test_json_decode_error(self) -> None:
        result = suggest("json.JSONDecodeError: Expecting value")
        assert result is not None
        assert "JSON" in result.title

    def test_permission_denied(self) -> None:
        result = suggest("PermissionError: [Errno 13] Permission denied")
        assert result is not None
        assert "permission" in result.title.lower()

    def test_no_match(self) -> None:
        result = suggest("some completely unknown error xyz123")
        assert result is None

    def test_exception_input(self) -> None:
        exc = ConnectionRefusedError("connection refused to task server")
        result = suggest(exc)
        assert result is not None

    def test_bootstrap_failed(self) -> None:
        result = suggest("Bootstrap failed: startup failure")
        assert result is not None
        assert "startup" in result.title.lower() or "Server" in result.title

    def test_stale_pid(self) -> None:
        result = suggest("Found stale PID file for orphan process")
        assert result is not None
        assert "stale" in result.title.lower() or "Stale" in result.title

    def test_git_conflict(self) -> None:
        result = suggest("CONFLICT (content): Merge conflict in src/main.py")
        assert result is not None
        assert "conflict" in result.title.lower() or "merge" in result.title.lower()


class TestFormatSuggestion:
    """Test suggestion formatting."""

    def test_format(self) -> None:
        s = ErrorSuggestion(
            pattern="test",
            title="Test error",
            steps=["Do step 1", "Do step 2"],
        )
        formatted = format_suggestion(s)
        assert "Test error" in formatted
        assert "1. Do step 1" in formatted
        assert "2. Do step 2" in formatted

    def test_suggest_and_format_match(self) -> None:
        result = suggest_and_format("Port 8052 already in use")
        assert result != ""
        assert "Port" in result

    def test_suggest_and_format_no_match(self) -> None:
        result = suggest_and_format("completely unknown error xyz")
        assert result == ""


class TestAllSuggestions:
    """Test the catalog accessor."""

    def test_returns_list(self) -> None:
        suggestions = all_suggestions()
        assert len(suggestions) >= 20

    def test_all_have_steps(self) -> None:
        for s in all_suggestions():
            assert len(s.steps) > 0, f"Suggestion '{s.title}' has no steps"
            assert s.title, f"Suggestion with pattern '{s.pattern}' has no title"
