## `bernstein trace export --anchor-endpoint` keeps a record checkable after 24 hours

A Trust Record's only time is `iat`, which the producer writes, and TRACE
verifiers reject a record older than 24 hours by default. The new opt-in
`--anchor-endpoint URL` submits each exported record to a time-anchor
service after it is written and saves the service's receipt beside it as
`<name>.anchor.json`. Bernstein recomputes the RFC 8785 sha256 of the
record and refuses a receipt that names any other digest; it is the
digest a child hop already names its parent by in `parent_record_hash`.
The option is off by default, has no built-in host, accepts `https://`
only (plain `http://` for loopback), and a failed anchor never removes the
export: the command exits `1` with the record still on disk. See
`docs/observability/trace-export.md#trace-anchor`.
