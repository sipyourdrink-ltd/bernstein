# MCP tool-definition observation

The gateway's opt-in `--observe-capabilities` probe records complete parsed
`tools/list` entries after initialization. Supply a stable `--server-name` and
configure the audit key as required by the gateway command. The observation
covers every top-level member, including `description`, schemas, annotations,
`title`, `_meta`, and future extension fields.

Each tool digest is `sha256:` followed by the hexadecimal SHA-256 of
`canonical_envelope_bytes(entry)`, using RFC 8785 canonical JSON. Object member
order is insignificant; arrays retain their order. The server digest hashes
canonical JSON of `{"profile":"mcp-tool-definitions-jcs-v1","tools":{name:digest}}`.
Canonical object ordering sorts the names without delimiter ambiguity.
The profile accepts JSON objects with string keys, arrays, strings, booleans,
null, finite floating-point numbers, and integers in the safe range
`[-(2^53-1), 2^53-1]`. Larger integer literals are rejected, including exactly
representable large values, rather than inheriting the shared canonicalizer's
legacy arbitrary-integer extension. Represent larger exact values as strings. Committed
vectors are in `tests/fixtures/mcp_tool_definition_vectors.json`.

The capability snapshot stores digests of definitions and individual top-level
members, rather than raw definitions. A changed observation adds
`tool_definitions` to the existing `mcp.capability_drift` event: profile,
previous/current server digests, and per-tool previous/current digests plus
`changed_fields`. Added and removed tools appear with a missing counterpart.
Existing top-level event digests continue to identify the sorted tool-name list.
Canonicalization and persistence run in a worker thread so optional observation
does not block ordinary proxy messages. The event is appended under the capability snapshot lock before replacing the
baseline; an audit failure leaves the previous baseline intact.

A legacy name-only snapshot has no previous definition digest. Its first full
observation records that unknown baseline explicitly and preserves first-contact
time. Invalid parsed entries, repeated names and non-canonical values leave the
previous baseline in place and produce a warning.

This is initialization-time observation. It does not block calls, approve a new
definition, re-list tools during a run, or pin prompts, initialization instructions
or the upstream launch identity. The parsed-entry layer cannot detect duplicate
JSON keys already discarded by the transport parser. Those enforcement and
transport changes remain tracked in #6984; this observation is not an approval
or an assertion that an upstream server is trusted.
