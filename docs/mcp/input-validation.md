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

## Schemas

Each tool's schema is a plain JSON Schema (Draft 7) file at
`src/bernstein/mcp/tool_schemas/<tool_name>.json`. The file's stem is the
tool name the schema applies to. Schemas are loaded once per process (on
first validation call) and cached; a malformed schema file fails fast at
load time via `jsonschema.Draft7Validator.check_schema`, not on the first
matching request.

The bundled server ships schemas for the core tool set:
`bernstein_health`, `bernstein_run`, `bernstein_status`, `bernstein_tasks`,
`bernstein_task_handle`, `bernstein_cost`, `bernstein_stop`,
`bernstein_approve`, `bernstein_create_subtask`, `bernstein_claim`,
`bernstein_update`, `load_skill`, `bernstein_context`,
`bernstein_post_artifact`, and the scenario-bridge tools
`bernstein_scenario`, `bernstein_scenario_status`, `bernstein_scenarios`.
Adding a new MCP tool means adding its schema file alongside these; a tool
with no schema file is unreachable (unknown-tool rejection), which is the
point of deny-by-default.

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
registry); `src/bernstein/mcp/tool_schemas/*.json` (per-tool schemas); wired
into request handling in `src/bernstein/mcp/server.py`
(`_validate_or_error`).
