## Catalog the governable surfaces a workspace declares

`bernstein govern inventory` reads the surfaces a workspace already declares —
MCP servers from `.mcp.json`, endpoints from a root OpenAPI or Swagger spec,
worktree roots from `bernstein.yaml` — and emits them as a JSON document with a
SHA-256 digest over the surface list, so an operator can say which surfaces were
present when a decision was taken. A config file the scan cannot interpret is
skipped rather than aborting the scan, so one broken spec no longer hides every
other source.

`bernstein govern validate` checks a governance playbook against the schema in
`core/governance/playbook.py`: unknown keys, malformed ids and versions,
duplicate surface or ceiling ids, and clauses whose `surface_ref` or
`ceiling_ref` resolves to nothing are each reported with the field that failed.
(#4973)
