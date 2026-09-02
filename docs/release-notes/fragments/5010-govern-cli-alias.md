## Unify governance CLI under `bernstein govern` (Issue #5010)

`bernstein govern` is now the canonical CLI surface for RBAC and budget
verification, govern plan generation, OTLP span ingestion, and governance
discovery. The `bernstein governance` command group is a deprecated alias
that forwards to `bernstein govern` and emits a deprecation warning; it will
be removed in v4.0.0.

**Diff stats:** +377 / -13 across 5 files.

**New CLI surface:**
- `bernstein govern verify <run>` — recompute RBAC and budget verdicts
- `bernstein govern plan` — generate a signed govern plan from playbook and inventory
- `bernstein govern ingest` — anchor OTLP spans from external runtimes
- `bernstein govern discover` — run governance discovery and draft a playbook

**Backward compatibility:**
- `bernstein governance verify` continues to work with a deprecation warning
- All subcommands forward to their `govern` equivalents

**Closes:** #5010
