"""Tests for saved HTTP/SSE MCP catalog entries (#6456)."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.capability_profile import (
    AdapterCapabilityProfile,
    InvocationSpec,
    ProfileImplementation,
    build_adapter_class_from_profile,
)
from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.adapters.cursor import CursorAdapter
from bernstein.adapters.plugin_sdk import (
    RemoteMCPRefusal,
    ensure_remote_mcp_supported,
    extract_remote_mcp_server_names,
)
from bernstein.core.agents.spawn_dry_run import SpawnDryRunValidator
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.protocols.mcp.mcp_manager import MCPManager, parse_server_configs
from bernstein.core.protocols.mcp.mcp_marketplace import marketplace_entries
from bernstein.core.protocols.mcp.mcp_protocol_test import run_protocol_test
from bernstein.core.protocols.mcp.mcp_registry import (
    MCPRegistry,
    MCPServerEntry,
    load_catalog_entries,
    resolve_header_env_vars,
    save_catalog_entries,
    validate_header_references,
)
from bernstein.core.protocols.mcp_catalog.manifest import CatalogValidationError, validate_catalog
from bernstein.core.security.network_policy import NetworkPolicyDenied

if TYPE_CHECKING:
    from pathlib import Path


def _make_fake_proc(pid: int = 4242) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None
    proc.returncode = None
    proc.stdin = None
    return proc


def test_saved_http_endpoint_survives_catalog_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_task: Any
) -> None:
    """Remote HTTP/SSE entries and legacy stdio entries round-trip without dummy packages."""
    monkeypatch.setenv("HTTP_API_TOKEN", "tok-http-secret")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "gh-secret")

    catalog_path = tmp_path / ".sdd" / "config" / "mcp_servers.yaml"
    http_entry = MCPServerEntry(
        name="internal-docs",
        url="https://mcp.internal.example/v1/mcp",
        headers={
            "Authorization": "Bearer ${HTTP_API_TOKEN}",
            "X-Workspace": "platform",
        },
        capabilities=("documentation", "search"),
        keywords=("internal docs", "runbook"),
    )
    sse_entry = MCPServerEntry(
        name="legacy-events",
        url="https://events.internal.example/sse",
        transport="sse",
        capabilities=("events",),
        keywords=("events",),
    )
    stdio_entry = MCPServerEntry(
        name="github",
        package="@modelcontextprotocol/server-github",
        capabilities=("git",),
        keywords=("pull request",),
        env_required=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        command="uvx",
        args=("mcp-server-github", "--read-only"),
    )

    save_catalog_entries(catalog_path, [http_entry, sse_entry, stdio_entry])
    reloaded = load_catalog_entries(catalog_path)
    assert len(reloaded) == 3

    reloaded_http, reloaded_sse, reloaded_stdio = reloaded
    assert reloaded_http.name == "internal-docs"
    assert reloaded_http.package == ""
    assert reloaded_http.url == "https://mcp.internal.example/v1/mcp"
    assert reloaded_http.transport == "http"
    assert reloaded_http.is_remote is True
    assert reloaded_http.headers == {
        "Authorization": "Bearer ${HTTP_API_TOKEN}",
        "X-Workspace": "platform",
    }

    assert reloaded_sse.url == "https://events.internal.example/sse"
    assert reloaded_sse.transport == "sse"
    assert reloaded_sse.is_remote is True

    assert reloaded_stdio.package == "@modelcontextprotocol/server-github"
    assert reloaded_stdio.transport == "stdio"
    assert reloaded_stdio.is_remote is False
    assert reloaded_stdio.command == "uvx"
    assert reloaded_stdio.args == ("mcp-server-github", "--read-only")

    raw_yaml = catalog_path.read_text(encoding="utf-8")
    assert "tok-http-secret" not in raw_yaml
    assert "${HTTP_API_TOKEN}" in raw_yaml

    registry = MCPRegistry(catalog_path)
    task = make_task(description="Search the runbook and events")
    selected = registry.resolve_for_tasks([task])
    assert selected is not None
    assert set(selected["mcpServers"].keys()) == {"internal-docs", "legacy-events"}
    assert selected["mcpServers"]["internal-docs"] == {
        "type": "http",
        "url": "https://mcp.internal.example/v1/mcp",
        "headers": {
            "Authorization": "Bearer tok-http-secret",
            "X-Workspace": "platform",
        },
    }
    assert selected["mcpServers"]["legacy-events"] == {
        "type": "sse",
        "url": "https://events.internal.example/sse",
    }


def test_invalid_transport_combinations_refuse_before_catalog_write(tmp_path: Path) -> None:
    """Invalid url/package/transport/header combos refuse before writing catalog."""
    catalog_path = tmp_path / ".sdd" / "config" / "mcp_servers.yaml"

    with pytest.raises(ValueError, match="cannot combine"):
        MCPServerEntry(
            name="mixed-pkg",
            package="@acme/mcp",
            url="https://mcp.example.com/mcp",
        )

    with pytest.raises(ValueError, match="cannot combine"):
        MCPServerEntry(
            name="mixed-cmd",
            url="https://mcp.example.com/mcp",
            command="node",
        )

    with pytest.raises(ValueError, match="requires a 'url'"):
        MCPServerEntry(name="missing-url", package="@acme/mcp", transport="http")

    with pytest.raises(ValueError, match="cannot declare HTTP 'headers' without 'url'"):
        MCPServerEntry(
            name="stdio-headers",
            package="@acme/mcp",
            headers={"X-Tenant": "acme"},
        )

    with pytest.raises(ValueError, match="must use transport 'http' or 'sse'"):
        MCPServerEntry(name="bad-transport", url="https://mcp.example.com/mcp", transport="websocket")

    with pytest.raises(ValueError, match="http\\(s\\) URL"):
        MCPServerEntry(name="bad-scheme", url="file:///tmp/mcp")

    # Constructing valid entry then mutating/attempting save with invalid literal secret fails before file creation.
    bad_entry = MCPServerEntry(
        name="ok-stdio",
        package="@acme/mcp",
    )
    assert bad_entry.transport == "stdio"

    with pytest.raises(ValueError, match="credential"):
        save_catalog_entries(
            catalog_path,
            [
                MCPServerEntry(
                    name="literal-secret",
                    url="https://mcp.example.com/mcp",
                    headers={"Authorization": "Bearer super-secret-literal"},
                )
            ],
        )
    assert not catalog_path.exists()

    # parse_server_configs preserves explicit SSE vs HTTP and infers /sse vs http.
    parsed = parse_server_configs(
        {
            "explicit_sse": {"transport": "sse", "url": "https://a.example/mcp"},
            "explicit_http_alias": {
                "transport": "streamable-http",
                "url": "https://b.example/sse",
                "headers": {"X-Tenant": "team-1"},
            },
            "inferred_sse": {"url": "https://c.example/v1/sse"},
            "inferred_http": {"url": "https://d.example/v1/mcp"},
        }
    )
    by_name = {cfg.name: cfg for cfg in parsed}
    assert by_name["explicit_sse"].transport == "sse"
    assert by_name["explicit_http_alias"].transport == "http"
    assert by_name["explicit_http_alias"].headers == {"X-Tenant": "team-1"}
    assert by_name["inferred_sse"].transport == "sse"
    assert by_name["inferred_http"].transport == "http"

    # Protocol test suite reports remote transport cleanly without running a subprocess.
    remote_entry = MCPServerEntry(name="remote-docs", url="https://mcp.example.com/mcp", transport="http")
    proto_result = asyncio.run(run_protocol_test(remote_entry))
    assert proto_result.passed is False
    assert proto_result.transport == "http"
    assert "stdio entries only" in proto_result.failures[0]


def test_selected_remote_config_survives_registry_manager_and_spawn_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_task: Any
) -> None:
    """Registry selection + base precedence + MCPManager layering reach Claude & Cursor adapters."""
    monkeypatch.setenv("DOCS_TOKEN", "tok-live-123")
    monkeypatch.setenv("MANAGER_TOKEN", "tok-mgr-456")

    catalog_path = tmp_path / ".sdd" / "config" / "mcp_servers.yaml"
    save_catalog_entries(
        catalog_path,
        [
            MCPServerEntry(
                name="docs",
                url="https://docs.example.com/mcp",
                transport="http",
                headers={"Authorization": "Bearer ${DOCS_TOKEN}", "X-Origin": "registry"},
                keywords=("documentation",),
            ),
            MCPServerEntry(
                name="unused-remote",
                url="https://unused.example.com/mcp",
                transport="sse",
                keywords=("unrelated-keyword",),
            ),
        ],
    )
    registry = MCPRegistry(catalog_path)
    manager = MCPManager(
        parse_server_configs(
            {
                "mgr-sse": {
                    "url": "https://mgr.example.com/sse",
                    "headers": {"X-Api-Key": "${MANAGER_TOKEN}"},
                }
            }
        )
    )
    manager.start_all()

    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    claude = ClaudeCodeAdapter()
    spawner = AgentSpawner(
        claude,
        templates_dir,
        tmp_path,
        use_worktrees=False,
        default_model="claude-sonnet-4-6",
        mcp_config={"mcpServers": {"docs": {"type": "http", "url": "https://base-override.example/mcp"}}},
        mcp_registry=registry,
        mcp_manager=manager,
    )

    task = make_task(description="Read documentation", mcp_servers=["mgr-sse"])
    captured_cmds: list[list[str]] = []

    def fake_popen(cmd: list[str], *args: Any, **kwargs: Any) -> MagicMock:
        captured_cmds.append(list(cmd))
        return _make_fake_proc(pid=9001 + len(captured_cmds))

    # Exercise ClaudeCodeAdapter directly via spawner with Popen mocked cleanly.
    with (
        patch.object(spawner, "_get_adapter_by_name", return_value=claude),
        patch.object(spawner, "_enforce_model_registry_gate"),
        patch.object(spawner, "_preflight_adapter_security_floor"),
        patch.object(spawner, "_preflight_adapter_admission"),
        patch.object(spawner, "_record_adapter_capability_selection"),
        patch.object(claude, "enforce_network_policy"),
        patch.object(claude, "_probe_fast_exit"),
        patch("bernstein.adapters.claude.subprocess.Popen", side_effect=fake_popen),
        patch("bernstein.adapters.claude.build_wrapper_script", return_value="print('ok')"),
    ):
        session = spawner.spawn_for_tasks([task])

    assert session.pid > 0
    claude_cmd = next(cmd for cmd in captured_cmds if "--mcp-config" in cmd)
    mcp_payload = json.loads(claude_cmd[claude_cmd.index("--mcp-config") + 1])
    servers = mcp_payload["mcpServers"]
    assert "unused-remote" not in servers
    # Base config overrides registry entry with the same name:
    assert servers["docs"]["url"] == "https://base-override.example/mcp"
    # MCPManager remote entry retains type, url, and expanded header:
    assert servers["mgr-sse"] == {
        "type": "sse",
        "url": "https://mgr.example.com/sse",
        "headers": {"X-Api-Key": "tok-mgr-456"},
    }

    # CursorAdapter also accepts remote MCP and writes resolved .cursor/mcp.json.
    cursor = CursorAdapter()
    cursor_workdir = tmp_path / "cursor-wd"
    cursor_workdir.mkdir()
    resolved_cursor_mcp = spawner._resolve_effective_mcp_for_tasks([task])
    ensure_remote_mcp_supported(cursor, resolved_cursor_mcp)
    with (
        patch.object(cursor, "enforce_network_policy"),
        patch.object(cursor, "_probe_fast_exit"),
        patch("bernstein.adapters.cursor.subprocess.Popen", return_value=_make_fake_proc(9100)),
    ):
        cursor.spawn(
            prompt="Inspect docs",
            workdir=cursor_workdir,
            model_config=ModelConfig(model="claude-sonnet-4-6", effort="normal"),
            session_id="worker-1",
            mcp_config=resolved_cursor_mcp,
        )

    written_cursor_mcp = json.loads((cursor_workdir / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert written_cursor_mcp["mcpServers"]["mgr-sse"]["headers"]["X-Api-Key"] == "tok-mgr-456"


def test_unsupported_adapter_refuses_selected_remote_server_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_task: Any
) -> None:
    """Unsupported, unknown, or profile-only adapters refuse remote MCP across spawn, retry, and resume."""
    monkeypatch.setenv("DOCS_TOKEN", "tok-secret")
    catalog_path = tmp_path / ".sdd" / "config" / "mcp_servers.yaml"
    save_catalog_entries(
        catalog_path,
        [
            MCPServerEntry(
                name="remote-docs",
                url="https://docs.example.com/mcp",
                transport="http",
                headers={"Authorization": "Bearer ${DOCS_TOKEN}"},
                keywords=("docs",),
            ),
            MCPServerEntry(
                name="local-stdio",
                package="@acme/stdio-server",
                keywords=("localtool",),
            ),
        ],
    )
    registry = MCPRegistry(catalog_path)

    unsupported_adapter = MagicMock(spec=CLIAdapter)
    unsupported_adapter.name.return_value = "unsupported-cli"

    profile_adapter = build_adapter_class_from_profile(
        AdapterCapabilityProfile(
            name="profile-only",
            display_name="Profile Only",
            implementation=ProfileImplementation.FACTORY,
            invocation=InvocationSpec(binary="profile-cli", prompt_flag="-p"),
            mcp_client=True,  # Generic mcp_client must NOT grant remote HTTP/SSE support.
        )
    )()
    ensure_remote_mcp_supported(ClaudeCodeAdapter(), {"mcpServers": {"remote-docs": {"url": "https://x.test"}}})
    with pytest.raises(RemoteMCPRefusal, match="supports_remote_mcp"):
        ensure_remote_mcp_supported(profile_adapter, {"mcpServers": {"remote-docs": {"url": "https://x.test"}}})

    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    spawner = AgentSpawner(
        unsupported_adapter,
        templates_dir,
        tmp_path,
        use_worktrees=False,
        default_model="mock-model",
        mcp_registry=registry,
    )

    remote_task = make_task(id="R1", description="Check docs endpoint")
    with pytest.raises(RemoteMCPRefusal, match="remote-docs"):
        spawner.spawn_for_tasks([remote_task])
    unsupported_adapter.spawn.assert_not_called()

    # Failover/retry to a second incapable adapter also refuses before child creation.
    primary_claude = ClaudeCodeAdapter()
    failover_adapter = MagicMock(spec=CLIAdapter)
    failover_adapter.name.return_value = "codex-fallback"
    spawner_failover = AgentSpawner(
        primary_claude,
        templates_dir,
        tmp_path,
        use_worktrees=False,
        default_model="mock-model",
        mcp_registry=registry,
    )
    with patch.object(spawner_failover, "_get_adapter_by_name", return_value=failover_adapter):
        with pytest.raises(RemoteMCPRefusal, match="codex-fallback"):
            spawner_failover.spawn_for_tasks([remote_task])
    failover_adapter.spawn.assert_not_called()

    # Resume path also refuses before adapter.spawn() or worktree adoption runs.
    resume_worktree = tmp_path / "preserved-wt"
    resume_worktree.mkdir()
    with pytest.raises(RemoteMCPRefusal, match="remote-docs"):
        spawner.spawn_for_resume(
            [remote_task],
            worktree_path=resume_worktree,
            changed_files=[],
        )
    unsupported_adapter.spawn.assert_not_called()

    # Control: stdio-only task and no-MCP task still succeed on adapters without SUPPORTS_REMOTE_MCP.
    unsupported_adapter.spawn.return_value = SpawnResult(pid=777, log_path=tmp_path / "stdio.log")
    stdio_task = make_task(id="S1", description="Use localtool helper")
    spawner.spawn_for_tasks([stdio_task])
    assert unsupported_adapter.spawn.call_count == 1
    assert "local-stdio" in unsupported_adapter.spawn.call_args.kwargs["mcp_config"]["mcpServers"]

    no_mcp_adapter = MagicMock(spec=CLIAdapter)
    no_mcp_adapter.name.return_value = "unsupported-cli"
    no_mcp_adapter.spawn.return_value = SpawnResult(pid=778, log_path=tmp_path / "nomcp.log")
    spawner_nomcp = AgentSpawner(
        no_mcp_adapter,
        templates_dir,
        tmp_path,
        use_worktrees=False,
        default_model="mock-model",
        mcp_registry=registry,
    )
    no_mcp_task = make_task(id="N1", description="Plain coding task")
    spawner_nomcp.spawn_for_tasks([no_mcp_task])
    assert no_mcp_adapter.spawn.call_count == 1


def test_credential_header_reference_is_resolved_without_persisting_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credential refs resolve only at runtime, stay off YAML/errors/previews, and fail cleanly when unset."""
    monkeypatch.delenv("MISSING_MCP_SECRET", raising=False)

    # Literal secret rejected without echoing secret text.
    with pytest.raises(ValueError) as exc_info:
        validate_header_references(
            {"Authorization": "Bearer raw-secret-value-999"},
            server_name="billing-mcp",
        )
    assert "raw-secret-value-999" not in str(exc_info.value)

    # Safe literal non-credential headers survive alongside ${VAR} credentials.
    validate_header_references(
        {
            "Authorization": "Bearer ${MISSING_MCP_SECRET}",
            "X-Workspace-Id": "ws-42",
        },
        server_name="billing-mcp",
    )

    # Missing env var refuses before connection or child creation, without leaking header template.
    entry = MCPServerEntry(
        name="billing-mcp",
        url="https://billing.example.com/mcp",
        transport="http",
        headers={
            "Authorization": "Bearer ${MISSING_MCP_SECRET}",
            "X-Workspace-Id": "ws-42",
        },
    )
    with pytest.raises(ValueError, match="MISSING_MCP_SECRET"):
        entry.to_mcp_config()

    # Resolved value returns cleanly once set, leaving YAML & dry-run check secret-free.
    catalog_path = tmp_path / ".sdd" / "config" / "mcp_servers.yaml"
    save_catalog_entries(catalog_path, [entry])
    monkeypatch.setenv("MISSING_MCP_SECRET", "synthetic-runtime-secret-abc")
    resolved_headers = resolve_header_env_vars(entry.headers, server_name=entry.name)
    assert resolved_headers == {
        "Authorization": "Bearer synthetic-runtime-secret-abc",
        "X-Workspace-Id": "ws-42",
    }
    assert "synthetic-runtime-secret-abc" not in catalog_path.read_text(encoding="utf-8")

    # Dry-run preview check only summarizes server count, never header values.
    preview = SpawnDryRunValidator(tmp_path)._check_mcp({"mcpServers": {"billing-mcp": entry.to_mcp_config()}})
    assert preview.passed is True
    assert "1 MCP server(s)" in preview.detail
    assert "synthetic-runtime-secret-abc" not in preview.detail


def test_plugin_namespacing_preserves_remote_endpoint_and_headers(tmp_path: Path) -> None:
    """Plugin namespacing preserves url, transport, headers, and collision isolation."""
    registry = MCPRegistry(tmp_path / "missing.yaml")
    alpha_entry = MCPServerEntry(
        name="docs",
        url="https://alpha.example.com/mcp",
        transport="http",
        headers={"X-Tenant": "alpha"},
        keywords=("alpha-docs",),
    )
    beta_entry = MCPServerEntry(
        name="docs",
        url="https://beta.example.com/sse",
        transport="sse",
        headers={"X-Tenant": "beta"},
        keywords=("beta-docs",),
    )

    registry.register_plugin_servers("plugin-a", [alpha_entry])
    registry.register_plugin_servers("plugin-b", [beta_entry])

    servers = {s.namespaced_name: s for s in registry.servers}
    assert set(servers.keys()) == {"plugin-a__docs", "plugin-b__docs"}
    assert servers["plugin-a__docs"].url == "https://alpha.example.com/mcp"
    assert servers["plugin-a__docs"].transport == "http"
    assert servers["plugin-a__docs"].headers == {"X-Tenant": "alpha"}
    assert servers["plugin-b__docs"].url == "https://beta.example.com/sse"
    assert servers["plugin-b__docs"].transport == "sse"
    assert servers["plugin-b__docs"].headers == {"X-Tenant": "beta"}


def test_remote_selection_respects_existing_network_and_offline_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_task: Any
) -> None:
    """Denied network policy blocks before spawn; unused remote entries add zero network calls."""
    catalog_path = tmp_path / ".sdd" / "config" / "mcp_servers.yaml"
    save_catalog_entries(
        catalog_path,
        [
            MCPServerEntry(
                name="unused-remote",
                url="https://never-connected.example.com/mcp",
                transport="http",
                keywords=("never-match-keyword",),
            )
        ],
    )
    registry = MCPRegistry(catalog_path)

    # Plain task does not select unused remote entry and succeeds offline on a stdio-only adapter.
    stdio_adapter = MagicMock(spec=CLIAdapter)
    stdio_adapter.name.return_value = "stdio-only"
    stdio_adapter.spawn.return_value = SpawnResult(pid=101, log_path=tmp_path / "ok.log")
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    spawner = AgentSpawner(
        stdio_adapter,
        templates_dir,
        tmp_path,
        use_worktrees=False,
        default_model="mock-model",
        mcp_registry=registry,
    )
    spawner.spawn_for_tasks([make_task(id="T-offline", description="Refactor local helper")])
    assert stdio_adapter.spawn.call_args.kwargs["mcp_config"] is None

    # Compatible adapter with denied network policy refuses via real policy check before subprocess launch.
    monkeypatch.setenv("BERNSTEIN_NETWORK_POLICY", "deny")
    claude = ClaudeCodeAdapter()
    denied_config = {"mcpServers": {"docs": {"type": "http", "url": "https://docs.example.com/mcp"}}}
    with patch("bernstein.adapters.claude.subprocess.Popen") as popen_mock:
        with pytest.raises(NetworkPolicyDenied):
            claude.spawn(
                prompt="Fetch docs",
                workdir=tmp_path,
                model_config=ModelConfig(model="claude-sonnet-4-6", effort="normal"),
                session_id="net-deny-1",
                mcp_config=denied_config,
            )
        popen_mock.assert_not_called()

    assert extract_remote_mcp_server_names(denied_config) == ("docs",)


def test_saved_http_support_does_not_add_bundled_vendor_or_mutate_signed_catalog() -> None:
    """Bundled marketplace and signed catalog manifest remain unchanged."""
    bundled_names = {entry.name for entry in marketplace_entries()}
    assert bundled_names == {"filesystem", "github", "fetch", "memory"}
    assert all(entry.transport == "stdio" and entry.url is None for entry in marketplace_entries())
    with pytest.raises(CatalogValidationError):
        validate_catalog({"version": 1})
