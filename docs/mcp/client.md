# Hardened MCP client

Bernstein consumes external MCP servers during orchestrator runs. The client
treats every upstream server as untrusted, brittle, and rate-limited: real
servers return malformed responses, hang mid-stream, demand re-auth on token
expiry, expose tools the client has no schema for, and occasionally misreport
their capability manifest. The client survives every one of these without
taking the orchestrator down.

The implementation lives in
`src/bernstein/core/protocols/mcp/mcp_client.py`
(`MCPClientSession`, `MCPClientManager`) and
`src/bernstein/core/cost/mcp_server_cost.py` (`MCPServerCostMeter`).

## Hardening features at a glance

| Feature | Entry point | Failure it contains |
|---------|-------------|---------------------|
| Capability-card validation | `call_tool` / `call_tool_streaming` | Server advertises a tool it cannot serve, or a caller asks for an undeclared tool. |
| Retry-with-continuation | `call_tool_streaming` | A streamed tool call drops mid-stream. |
| Streamed-output cancellation | `StreamedToolCall.cancel` | A long call must be aborted without leaking the request; partial output is kept. |
| Per-server cost-meter | `MCPServerCostMeter` | MCP spend must be attributed and capped per server per task. |
| Schema-violation containment | every call path | Invalid JSON or missing fields; the server is quarantined for the task. |

## Stateless wire identity

The client is stateless on the wire: it never mints a protocol session id and
never round-trips the removed `Mcp-Session-Id` header (a legacy header a
server still sends is ignored). Instead, every request and notification
carries a per-request `_meta` built by `build_request_meta`
(`src/bernstein/core/protocols/mcp/stateless_core.py`):

- `traceparent` carries a W3C trace id derived from the run root hash and a
  span id derived from the call's content hash plus its ordered call index;
- `baggage` carries the method and the call index;
- `client.capabilities` advertises the client capability map per request,
  replacing the `initialize` handshake advertisement.

Because every id is a pure function of run identity and call content, two
replays of the same run emit byte-identical `_meta`, and any server instance
can serve any request. Pass `run_root_hash` (the run journal's genesis hash)
to `MCPClientSession` to bind wire traces to the run; without it, a
deterministic projection of the server identity keeps replays reproducible.

A tool result of type `input_required` is not an error: the server's opaque
`requestState` echo surfaces on `result.metadata["request_state"]` alongside
the prompt. Retry by calling `call_tool` again with the supplied input as
`arguments` and `request_state=<the echo>`; the echoed state carries
everything needed to resume, so the retry may be served by a different
server instance (or issued from a fresh client) with no shared memory.

## Capability-card validation

Before issuing a tool call the client verifies the tool is declared in the
server's manifest (the tool list discovered at `connect` time, cached on the
session). A mismatch raises `MCPCapabilityMissing`, logged with the manifest
digest so the rejection can be correlated against the exact manifest the
client validated against.

```python
session = MCPClientSession(RemoteServerConfig(name="github", url="https://..."))
await session.connect()           # discovers + digests the manifest
await session.call_tool("create_issue", {...})   # ok: declared
await session.call_tool("rm_minus_rf", {...})     # raises MCPCapabilityMissing
```

`MCPCapabilityMissing` subclasses `MCPToolNotFoundError`, so callers that
already catch the broader not-found error keep working; callers that want the
manifest-digest context catch the subclass. The manifest digest is exposed on
`session.manifest_digest` (a SHA-256 hex string). Validation can be disabled
per server with `RemoteServerConfig(validate_capabilities=False)`.

## Retry-with-continuation

`call_tool_streaming` survives mid-stream drops. Chunks are produced by a
`stream_factory` callable invoked with `(resumption_token, idempotency_key)`:

- On the first attempt the resumption token is `None`.
- If the stream drops and the server emitted a checkpoint token, the client
  resumes from that token (`StreamChunk.checkpoint_token`).
- If the server does not support resumption, the client replays the whole
  call carrying the same idempotency key so the server can deduplicate.

Retries are bounded by `RemoteServerConfig.max_continuation_retries`. When
they are exhausted the client raises `MCPStreamDropped` and marks the server
degraded.

```python
def stream_factory(resume_from, idempotency_key):
    # resume_from is the last checkpoint token, or None on first attempt.
    return transport.open_stream(resume_from=resume_from, idem=idempotency_key)

result = await session.call_tool_streaming("long_query", {...}, stream_factory=stream_factory)
```

A `StreamChunk` carries `text`, an optional `checkpoint_token`, a `final`
flag, and a `dropped` flag. A stream that ends without a `final` chunk is
treated as a drop so the client retries rather than silently truncating.

## Streamed-output cancellation

Pass a caller-owned `StreamedToolCall` handle to `call_tool_streaming` and
call `handle.cancel()` from another task. The consuming loop observes the flag
at the next chunk boundary, stops without leaking the underlying request, and
preserves the text seen so far on `handle.partial_content`. The returned
`ToolCallResult.metadata["cancelled"]` is `True` and `content` holds the
partial output.

```python
handle = StreamedToolCall(server_name="github", tool_name="long_query")
task = asyncio.create_task(
    session.call_tool_streaming("long_query", {...}, stream_factory=f, handle=handle)
)
handle.cancel()              # cooperative; partial output is preserved
result = await task
assert result.metadata["cancelled"] is True
```

## Per-server cost-meter

`MCPServerCostMeter` accumulates MCP spend per `(task_id, server_name)` pair
and wires into the existing `core/cost/` subsystem. Construct the meter with
an optional `SpendLedger`; metered calls are then also flushed into that
ledger tagged with the server name (`tags.extra["mcp_server"]`) under the
synthetic model label `mcp-server`, so the normal cost rollups
(`ledger.totals_by("task")`) include MCP spend.

```python
from bernstein.core.cost.mcp_server_cost import MCPServerCostMeter
from bernstein.core.cost.spend_ledger import SpendLedger

meter = MCPServerCostMeter(ledger=SpendLedger(path=...))
manager = MCPClientManager(cost_meter=meter, task_id="task-42")
await manager.connect(config)
await manager.call_tool("github", "search", {...}, cost_usd=0.02)

manager.server_cost("github")   # 0.02
manager.task_cost()             # total across all servers for task-42
```

Negative costs are clamped to zero so a misreporting server cannot corrupt
the rolling totals. A ledger failure is logged and swallowed: accounting
never takes the client down.

## Schema-violation containment

Malformed responses are caught and surfaced as `MCPSchemaViolation`. This
covers invalid JSON, a non-object JSON-RPC envelope, a non-object `result`
field, a `tools/list` entry missing its `name`, and a malformed tool-result
`content` block. On any of these the client:

1. Marks the server degraded for the rest of the task
   (`session.is_degraded`, `session.degraded_reason`).
2. Surfaces the failure through the metrics tracker (`MCPMetricsCollector`):
   the call is recorded as an error and the server availability is flipped.
3. Raises the typed `MCPSchemaViolation` rather than leaking a raw decode
   exception.

`MCPClientManager.degraded_servers()` lists every server currently
quarantined for the task. Degradation is sticky for the task: the first
reason is retained and the flag is not cleared by later successful calls.

## Wiring metrics and cost into the manager

`MCPClientManager` threads a shared cost meter, a metrics collector, and a
`task_id` into every session it opens:

```python
manager = MCPClientManager(
    cost_meter=MCPServerCostMeter(ledger=ledger),
    metrics=MCPMetricsCollector(),
    task_id="task-42",
)
```

## Tests

`tests/unit/mcp/test_client_hardened.py` exercises every acceptance path with
a scriptable in-memory fake server (`FakeMCPServer`): capability validation,
checkpoint resume, full retry with idempotency key, retry exhaustion,
cancellation, cost accumulation and ledger flush, and each schema-violation
shape. The legacy surface remains covered by `tests/unit/test_mcp_client.py`.
