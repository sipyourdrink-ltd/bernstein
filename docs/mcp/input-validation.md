# MCP tool-call input validation

Every tool-call payload that reaches the Bernstein MCP server is
shape-validated before the tool handler runs. The validator is the
orchestrator-side input firewall for the MCP transport: unknown tools are
refused, unknown top-level properties are refused, and each declared tool's
arguments are checked against a JSON Schema before the handler ever sees
them. This is deny-by-default — a payload has to positively match a
registered schema to be dispatched, rather than being let through unless it
trips a blocklist rule.

## How it behaves

Validation runs in this order for every incoming `tools/call`:

1. **Unknown tool check.** If no schema is registered for the tool name, the
   call is rejected with JSON-RPC code `-32601` (method not found).
2. **Payload shape check.** `params` must be a JSON object; anything else is
   rejected with `-32602` (invalid params).
3. **Deny-by-default rules**, applied before JSON Schema validation so the
   schema validator never has to parse a pathological payload:
   - **Size cap** — serialized payload over 64 KiB is rejected.
   - **Recursion cap** — nested dicts/lists deeper than 10 levels are
     rejected.
   - **Control-character filter** — string arguments containing C0/C1
     control characters are rejected, except plain tab, newline, and
     carriage return (so free-form multi-line fields such as `goal=` keep
     working). This blocks prompt-injected ANSI escapes and stray control
     bytes from reaching a downstream agent's terminal rendering.
4. **JSON Schema validation** against the tool's registered `Draft7`
   schema. Every violation is collected (not just the first) and returned
   as a `{path, reason}` list, where `path` is a JSON pointer to the
   offending field.

A clean payload becomes a `ValidatedPayload(tool_name, payload)`; a failure
becomes a `ValidationError(tool_name, code, message, errors)`, which the MCP
server renders as a JSON-RPC 2.0 error object via `to_jsonrpc_error()`.

## Validation scope

This validator runs on the tools registered in `bernstein.mcp.server`, which
serves the stdio and SSE transports.

The streamable HTTP transport in `bernstein.mcp.remote_transport` is a
separate implementation and does **not** call `validate_tool_call`. Over that
transport the size cap, recursion cap, control-character filter and JSON
Schema checks are all absent, and the 8 tools it exposes carry schemas
restated inside that module rather than loaded from the schema files below.
Starting that transport logs a warning saying so. Do not read this page as a
statement about what an internet-reachable streamable HTTP deployment
enforces. Bringing both transports onto one registry and one validation path
is tracked in issue #3083; this note goes away with that change.

## Schemas

Each tool's schema is a plain JSON Schema (Draft 7) file at
`src/bernstein/mcp/tool_schemas/<tool_name>.json`. The file's stem is the
tool name the schema applies to. Schemas are loaded once per process (on
first validation call) and cached; a malformed schema file fails fast at
load time via `jsonschema.Draft7Validator.check_schema`, not on the first
matching request.

The bundled server ships schemas for the core tool set:
`bernstein_run`, `bernstein_status`, `bernstein_run_status`,
`bernstein_approve`, `bernstein_complete`, `bernstein_cancel`,
`bernstein_claim`, `bernstein_post_message`, `bernstein_post_artifact`,
`bernstein_task_capsule`, `bernstein_shutdown_orchestrator`, `load_skill`,
`bernstein_scenario`, `bernstein_verify_lineage`, and every deprecated
alias kept callable for the transition release (each alias validates
against its own historical schema file).
Adding a new MCP tool means adding its schema file alongside these; a tool
with no schema file is unreachable (unknown-tool rejection), which is the
point of deny-by-default.

## One schema per tool, not two

The schema file is also the schema advertised to clients. After tool
registration, `_apply_advertised_schemas()` in
`src/bernstein/mcp/server.py` replaces each tool's `inputSchema` with the
schema `validate_tool_call` enforces, so a caller sees
`scope: enum [small, medium, large]` rather than the bare `scope: string`
FastMCP derives from the Python signature. Without this, every constrained
argument is a guaranteed first-call failure: the caller sends a plausible
value and gets a rejection it had no way to predict.

Consequences worth knowing:

- `bernstein_post_artifact` advertises its `allOf` conditional, so a client
  can see that a `report` needs `body`, a `table` needs `columns` and
  `rows`, and a `link` needs `url` and `link_kind`. A handler argument left
  at its empty-string default counts as "not supplied" and is not validated
  as if the caller had sent it.
- Only the advertised copy is replaced. Argument coercion still runs through
  FastMCP's signature-derived model, and enforcement still runs through
  `validate_tool_call` inside each handler.
- The advertised schema is a deep copy, so a client-side mutation cannot
  reach the process-wide registry the validator reads.

`tests/unit/mcp/test_advertised_schema_parity.py` is the drift guard. It
asserts, per tool, that the advertised and enforced schemas agree on every
constrained field, and that the set of schema file stems equals the set of
registered tool names in both directions. Tightening enforcement without
updating what is advertised fails the build.

## Permissive mode (migration aid)

Set `BERNSTEIN_MCP_VALIDATION=permissive` to log rejected payloads instead of
refusing them — the call is still logged with its tool name, error code, and
the full violation list, but a best-effort `ValidatedPayload` is returned so
the handler runs anyway. This exists only to ease a migration where a client
sends payloads the current schemas don't yet accept; it is not intended as a
steady-state production setting, and treating a call as validated in
permissive mode does not mean the arguments were actually checked.

The default (and every other value of the env var) is strict mode: every
rejection is enforced.

## Limitations

- `SchemaRegistry` supports an `allow_unsafe_args` exemption set (tool names
  skipped by the size/depth/control-character deny rules, though schema
  validation still runs on them), but the bundled server registry does not
  currently populate it from `bernstein.yaml` — there is no config key today
  that exempts a specific tool from the deny rules in the shipped server.
  The exemption mechanism exists for callers that build their own
  `SchemaRegistry` directly.
- Validation covers the shape of the arguments, not their semantics — a
  syntactically valid `bernstein_run` payload can still name a task that
  fails for unrelated reasons downstream.

## Source

`src/bernstein/mcp/input_validation.py` (validator, deny rules, schema
registry, the shared `validate_or_error` / `validation_error_response`
handler helpers); `src/bernstein/mcp/tool_schemas/*.json` (per-tool
schemas); wired into request handling in `src/bernstein/mcp/server.py`
(`_validate_or_error`) and advertised from the same files by
`_apply_advertised_schemas`. `bernstein_verify_lineage` validates in
`src/bernstein/mcp/resources/lineage.py`.
