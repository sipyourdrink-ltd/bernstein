"""Tests for MCP config validation on startup (MCP-002)."""

from __future__ import annotations

from unittest.mock import patch

from bernstein.core.mcp_config_validator import (
    McpConfigError,
    check_command_exists,
    check_env_vars,
    check_url_reachable,
    validate_mcp_configs,
)
from bernstein.core.mcp_manager import MCPServerConfig

# ---------------------------------------------------------------------------
# check_command_exists
# ---------------------------------------------------------------------------


class TestCheckCommandExists:
    """Tests for command-in-PATH validation."""

    def test_sse_transport_skipped(self) -> None:
        cfg = MCPServerConfig(name="remote", url="http://x", transport="sse")
        assert check_command_exists(cfg) is None

    def test_empty_command(self) -> None:
        cfg = MCPServerConfig(name="empty", command=[])
        err = check_command_exists(cfg)
        assert err is not None
        assert err.check == "command_missing"

    @patch("bernstein.core.protocols.mcp_config_validator.shutil.which", return_value="/usr/bin/npx")
    def test_command_found(self, _mock_which: object) -> None:
        cfg = MCPServerConfig(name="github", command=["npx", "-y", "gh-mcp"])
        assert check_command_exists(cfg) is None

    @patch("bernstein.core.protocols.mcp_config_validator.shutil.which", return_value=None)
    def test_command_not_found(self, _mock_which: object) -> None:
        cfg = MCPServerConfig(name="missing", command=["nonexistent-bin"])
        err = check_command_exists(cfg)
        assert err is not None
        assert err.check == "command_not_found"
        assert "nonexistent-bin" in err.message


# ---------------------------------------------------------------------------
# check_env_vars
# ---------------------------------------------------------------------------


class TestCheckEnvVars:
    """Tests for environment variable validation."""

    def test_no_env_vars(self) -> None:
        cfg = MCPServerConfig(name="basic", command=["echo"])
        assert check_env_vars(cfg) == []

    def test_literal_values_pass(self) -> None:
        cfg = MCPServerConfig(
            name="literal",
            command=["echo"],
            env={"KEY": "actual_value"},
        )
        assert check_env_vars(cfg) == []

    @patch.dict("os.environ", {"GITHUB_TOKEN": "tok123"})
    def test_reference_present(self) -> None:
        cfg = MCPServerConfig(
            name="github",
            command=["npx"],
            env={"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
        )
        assert check_env_vars(cfg) == []

    @patch.dict("os.environ", {}, clear=True)
    def test_reference_missing(self) -> None:
        cfg = MCPServerConfig(
            name="github",
            command=["npx"],
            env={"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
        )
        errors = check_env_vars(cfg)
        assert len(errors) == 1
        assert errors[0].check == "env_var_missing"
        assert "GITHUB_TOKEN" in errors[0].message

    @patch.dict("os.environ", {"A": "1"}, clear=True)
    def test_multiple_refs_partial_missing(self) -> None:
        cfg = MCPServerConfig(
            name="multi",
            command=["npx"],
            env={"A": "${A}", "B": "${B_MISSING}"},
        )
        errors = check_env_vars(cfg)
        assert len(errors) == 1
        assert "B_MISSING" in errors[0].message


# ---------------------------------------------------------------------------
# check_url_reachable
# ---------------------------------------------------------------------------


class TestCheckUrlReachable:
    """Tests for URL reachability checks."""

    def test_stdio_transport_skipped(self) -> None:
        cfg = MCPServerConfig(name="stdio", command=["echo"])
        assert check_url_reachable(cfg) is None

    def test_sse_missing_url(self) -> None:
        cfg = MCPServerConfig(name="bad", transport="sse")
        err = check_url_reachable(cfg)
        assert err is not None
        assert err.check == "url_missing"

    @patch("urllib.request.urlopen")
    def test_url_reachable(self, mock_urlopen: object) -> None:
        cfg = MCPServerConfig(
            name="remote",
            url="http://localhost:9090/sse",
            transport="sse",
        )
        assert check_url_reachable(cfg) is None

    @patch(
        "urllib.request.urlopen",
        side_effect=ConnectionError("refused"),
    )
    def test_url_unreachable(self, _mock_urlopen: object) -> None:
        cfg = MCPServerConfig(
            name="remote",
            url="http://localhost:9090/sse",
            transport="sse",
        )
        err = check_url_reachable(cfg)
        assert err is not None
        assert err.check == "url_unreachable"
        assert "not reachable" in err.message


# ---------------------------------------------------------------------------
# validate_mcp_configs (aggregate)
# ---------------------------------------------------------------------------


class TestValidateMcpConfigs:
    """Tests for the aggregate validation function."""

    @patch("bernstein.core.protocols.mcp_config_validator.shutil.which", return_value="/usr/bin/echo")
    def test_all_valid(self, _mock_which: object) -> None:
        configs = [
            MCPServerConfig(name="test", command=["echo", "hello"]),
        ]
        errors = validate_mcp_configs(configs, check_urls=False)
        assert errors == []

    @patch("bernstein.core.protocols.mcp_config_validator.shutil.which", return_value=None)
    def test_collects_multiple_errors(self, _mock_which: object) -> None:
        configs = [
            MCPServerConfig(name="bad1", command=["missing1"]),
            MCPServerConfig(name="bad2", command=["missing2"]),
        ]
        errors = validate_mcp_configs(configs, check_urls=False)
        assert len(errors) == 2
        names = {e.server_name for e in errors}
        assert names == {"bad1", "bad2"}

    def test_empty_configs(self) -> None:
        errors = validate_mcp_configs([])
        assert errors == []

    @patch("bernstein.core.protocols.mcp_config_validator.shutil.which", return_value="/usr/bin/npx")
    def test_skip_url_check(self, _mock_which: object) -> None:
        configs = [
            MCPServerConfig(name="stdio", command=["npx"]),
            MCPServerConfig(name="sse", url="http://bad-url", transport="sse"),
        ]
        # With check_urls=False, URL errors are not checked
        errors = validate_mcp_configs(configs, check_urls=False)
        assert all(e.check != "url_unreachable" for e in errors)

    def test_mcp_config_error_str(self) -> None:
        err = McpConfigError(
            server_name="github",
            check="command_not_found",
            message="Command 'npx' not found in PATH",
        )
        s = str(err)
        assert "[github]" in s
        assert "command_not_found" in s
        assert "npx" in s
