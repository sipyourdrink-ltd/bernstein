## HTTP and SSE MCP Catalog Support

Added optional remote HTTP (`http`) and SSE (`sse`) `url`, `transport`, and `${ENV_VAR}` `headers` support to `.sdd/config/mcp_servers.yaml` (`MCPServerEntry` and `MCPManager` / `MCPServerConfig`), rejecting literal credential headers and enforcing `AdapterCapability.SUPPORTS_REMOTE_MCP` before spawn, failover, or resume so incompatible adapters refuse cleanly with `RemoteMCPRefusal` (#6456).
