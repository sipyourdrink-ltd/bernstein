## MCP capability drift audit events

Bernstein now records `mcp.capability_drift` events on the HMAC-chained audit chain whenever an MCP server's tool set changes.

Each event carries a content-addressed capability digest (SHA-256 of the sorted canonical tool-name list), the server name, run ID, and explicit `added_tools` / `removed_tools` sets. On first contact the previous digest is `None`; subsequent contacts compare against the stored digest and emit a delta.

A persistent `ServerCapabilitiesStore` holds the last-seen digest per server. This means the audit chain can answer "what could this server do on the day of that run" retroactively — a G7 security property — and drift evidence is queryable via `chain.query(event_type=EVENT_MCP_CAPABILITY_DRIFT)`.
