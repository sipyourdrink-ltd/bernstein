"""Tests for MCP transport abstraction (MCP-004)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.mcp_transport import (
    McpTransport,
    SseTransport,
    StdioTransport,
    StreamableHttpTransport,
    TransportConfig,
    TransportError,
    create_transport,
    list_transports,
    register_transport,
    reset_transport_registry,
)

# ---------------------------------------------------------------------------
# TransportConfig
# ---------------------------------------------------------------------------


class TestTransportConfig:
    """Tests for TransportConfig dataclass."""

    def test_defaults(self) -> None:
        cfg = TransportConfig()
        assert cfg.command == []
        assert cfg.url == ""
        assert cfg.env == {}
        assert cfg.headers == {}
        assert cfg.timeout == pytest.approx(10.0)

    def test_custom_values(self) -> None:
        cfg = TransportConfig(
            command=["npx", "-y", "server"],
            url="http://localhost:8080",
            env={"TOKEN": "abc"},
            headers={"Authorization": "Bearer xyz"},
            timeout=5.0,
        )
        assert cfg.command == ["npx", "-y", "server"]
        assert cfg.url == "http://localhost:8080"
        assert cfg.timeout == pytest.approx(5.0)

    def test_frozen(self) -> None:
        cfg = TransportConfig()
        with pytest.raises(AttributeError):
            cfg.url = "nope"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# StdioTransport
# ---------------------------------------------------------------------------


class TestStdioTransport:
    """Tests for StdioTransport."""

    def test_protocol_compliance(self) -> None:
        transport = StdioTransport()
        assert isinstance(transport, McpTransport)

    def test_transport_type(self) -> None:
        assert StdioTransport().transport_type == "stdio"

    def test_not_connected_initially(self) -> None:
        transport = StdioTransport()
        assert transport.is_connected is False

    def test_health_check_when_disconnected(self) -> None:
        transport = StdioTransport()
        assert transport.health_check() is False

    def test_connect_empty_command_raises(self) -> None:
        transport = StdioTransport()
        with pytest.raises(TransportError, match="non-empty command"):
            transport.connect(TransportConfig(command=[]))

    @patch("bernstein.core.protocols.mcp_transport.subprocess.Popen")
    def test_connect_spawns_process(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        transport = StdioTransport()
        transport.connect(TransportConfig(command=["npx", "-y", "server"]))

        assert transport.is_connected is True
        assert transport.process is not None
        mock_popen.assert_called_once()

    @patch("bernstein.core.protocols.mcp_transport.subprocess.Popen")
    def test_health_check_alive(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        transport = StdioTransport()
        transport.connect(TransportConfig(command=["echo"]))
        assert transport.health_check() is True

    @patch("bernstein.core.protocols.mcp_transport.subprocess.Popen")
    def test_health_check_dead(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = 1
        mock_popen.return_value = mock_proc

        transport = StdioTransport()
        transport.connect(TransportConfig(command=["echo"]))
        assert transport.health_check() is False

    @patch("bernstein.core.protocols.mcp_transport.subprocess.Popen")
    def test_disconnect_terminates(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        transport = StdioTransport()
        transport.connect(TransportConfig(command=["echo"]))
        transport.disconnect()

        mock_proc.terminate.assert_called_once()
        assert transport.is_connected is False
        assert transport.process is None

    def test_disconnect_when_not_connected(self) -> None:
        transport = StdioTransport()
        transport.disconnect()  # no-op, no error

    @patch("bernstein.core.protocols.mcp_transport.subprocess.Popen")
    def test_connect_with_env(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        transport = StdioTransport()
        transport.connect(TransportConfig(command=["echo"], env={"MY_VAR": "val"}))

        call_kwargs = mock_popen.call_args.kwargs
        assert call_kwargs["env"] is not None
        assert call_kwargs["env"]["MY_VAR"] == "val"

    @patch("bernstein.core.protocols.mcp_transport.subprocess.Popen")
    def test_connect_failure_raises_transport_error(self, mock_popen: MagicMock) -> None:
        mock_popen.side_effect = FileNotFoundError("not found")

        transport = StdioTransport()
        with pytest.raises(TransportError, match="Failed to spawn"):
            transport.connect(TransportConfig(command=["nonexistent"]))


# ---------------------------------------------------------------------------
# SseTransport
# ---------------------------------------------------------------------------


class TestSseTransport:
    """Tests for SseTransport."""

    def test_protocol_compliance(self) -> None:
        assert isinstance(SseTransport(), McpTransport)

    def test_transport_type(self) -> None:
        assert SseTransport().transport_type == "sse"

    def test_not_connected_initially(self) -> None:
        transport = SseTransport()
        assert transport.is_connected is False

    def test_connect_empty_url_raises(self) -> None:
        transport = SseTransport()
        with pytest.raises(TransportError, match="non-empty url"):
            transport.connect(TransportConfig(url=""))

    def test_connect_stores_url(self) -> None:
        transport = SseTransport()
        transport.connect(TransportConfig(url="http://example.com/sse"))
        assert transport.is_connected is True
        assert transport.url == "http://example.com/sse"

    def test_disconnect(self) -> None:
        transport = SseTransport()
        transport.connect(TransportConfig(url="http://example.com/sse"))
        transport.disconnect()
        assert transport.is_connected is False
        assert transport.url == ""

    def test_health_check_when_disconnected(self) -> None:
        transport = SseTransport()
        assert transport.health_check() is False


# ---------------------------------------------------------------------------
# StreamableHttpTransport
# ---------------------------------------------------------------------------


class TestStreamableHttpTransport:
    """Tests for StreamableHttpTransport."""

    def test_protocol_compliance(self) -> None:
        assert isinstance(StreamableHttpTransport(), McpTransport)

    def test_transport_type(self) -> None:
        assert StreamableHttpTransport().transport_type == "streamable_http"

    def test_not_connected_initially(self) -> None:
        transport = StreamableHttpTransport()
        assert transport.is_connected is False

    def test_connect_empty_url_raises(self) -> None:
        transport = StreamableHttpTransport()
        with pytest.raises(TransportError, match="non-empty url"):
            transport.connect(TransportConfig(url=""))

    def test_connect_stores_url_and_headers(self) -> None:
        transport = StreamableHttpTransport()
        transport.connect(
            TransportConfig(
                url="http://example.com/mcp",
                headers={"Authorization": "Bearer tok"},
            )
        )
        assert transport.is_connected is True
        assert transport.url == "http://example.com/mcp"

    def test_disconnect(self) -> None:
        transport = StreamableHttpTransport()
        transport.connect(TransportConfig(url="http://example.com/mcp"))
        transport.disconnect()
        assert transport.is_connected is False
        assert transport.url == ""

    def test_health_check_when_disconnected(self) -> None:
        transport = StreamableHttpTransport()
        assert transport.health_check() is False


# ---------------------------------------------------------------------------
# Transport factory
# ---------------------------------------------------------------------------


class TestTransportFactory:
    """Tests for transport registration and creation."""

    def setup_method(self) -> None:
        reset_transport_registry()

    def teardown_method(self) -> None:
        reset_transport_registry()

    def test_list_builtins(self) -> None:
        names = list_transports()
        assert "stdio" in names
        assert "sse" in names
        assert "streamable_http" in names

    def test_create_stdio(self) -> None:
        transport = create_transport("stdio")
        assert transport.transport_type == "stdio"
        assert isinstance(transport, StdioTransport)

    def test_create_sse(self) -> None:
        transport = create_transport("sse")
        assert transport.transport_type == "sse"
        assert isinstance(transport, SseTransport)

    def test_create_streamable_http(self) -> None:
        transport = create_transport("streamable_http")
        assert transport.transport_type == "streamable_http"
        assert isinstance(transport, StreamableHttpTransport)

    def test_create_unknown_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown transport"):
            create_transport("grpc")

    def test_register_custom(self) -> None:
        class GrpcTransport:
            @property
            def transport_type(self) -> str:
                return "grpc"

            @property
            def is_connected(self) -> bool:
                return False

            def connect(self, config: object) -> None:
                pass

            def health_check(self) -> bool:
                return False

            def disconnect(self) -> None:
                pass

        register_transport("grpc", GrpcTransport)
        assert "grpc" in list_transports()

        transport = create_transport("grpc")
        assert transport.transport_type == "grpc"

    def test_register_duplicate_raises(self) -> None:
        with pytest.raises(ValueError, match="already registered"):
            register_transport("stdio", StdioTransport)

    def test_reset_clears_custom(self) -> None:
        class CustomTransport:
            @property
            def transport_type(self) -> str:
                return "custom"

            @property
            def is_connected(self) -> bool:
                return False

            def connect(self, config: object) -> None:
                pass

            def health_check(self) -> bool:
                return False

            def disconnect(self) -> None:
                pass

        register_transport("custom", CustomTransport)
        assert "custom" in list_transports()
        reset_transport_registry()
        assert "custom" not in list_transports()
        # Built-ins should survive
        assert "stdio" in list_transports()
