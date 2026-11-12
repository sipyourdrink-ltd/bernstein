# Volunteer task runner

How a claimed volunteer task is executed end-to-end, and how each stage can refuse.

## Execution flow

```
1. Parse URL  →  2. Fetch manifest  →  3. Derive sandbox  →  4. Execute task  →  5. Produce receipt
```

### Stage 1: Parse URL

The runner accepts GitHub issue URLs in the form:

```
https://github.com/owner/project/issues/123
```

Validation checks:
- Host is `github.com` (or a configured GHE instance)
- Path segments match the issue pattern
- Issue carries the project's `task_label` (e.g. `volunteer-ok`)

If any check fails, the runner refuses immediately with a signed refusal receipt.

### Stage 2: Fetch manifest

The worker clones the repo (shallow, single branch) and reads `.bernstein/volunteer.json` at the commit the issue references. The manifest is parsed and validated against the known schema.

If the manifest is absent, malformed, or its `status` field is not `"active"`, the runner refuses.

### Stage 3: Derive sandbox

The sandbox profile is a deterministic function of:
- The manifest's `sandbox` field
- `allowed_paths`
- `egress_allowlist`
- `max_wall_clock_minutes`
- The donor's optional wall-clock and memory ceilings passed to the runner

The signed result bundle records the derived profile digest in `sandbox_profile`.

### Stage 4: Execute task

The task body (issue description + comments) is never executed as shell code. The worker:

1. Checks out the repo at the issue's base commit
2. Runs the agent in the worktree with a filtered environment and a wall-clock cap
3. Runs the manifest's `gates` (e.g. `["uv", "run", "pytest", "-q"]`)
4. Records stdout/stderr, wall-clock time, and exit code

If any gate fails, execution stops and a partial receipt is produced.

### Stage 5: Produce receipt

The finishing step returns a signed result bundle with the task identity,
patch, gate results, `manifest_sha256`, and `sandbox_profile`. Use
`bernstein volunteer verify-bundle` to rerun the gates from the bundle's
attested base commit.

## Refusal paths

A refusal receipt is produced instead of a success receipt when:

| Stage | Refusal reason |
|-------|----------------|
| 1 | Invalid URL or host not allowed |
| 2 | Manifest absent or inactive |
| 3 | Sandbox derivation fails (e.g. unknown `sandbox` type) |
| 4 | Task exceeds its wall-clock budget |

Refusal receipts carry the same structure as success receipts but have `status: "refused"` and a `refusal_reason` field.

## Verifying a receipt

```bash
bernstein volunteer verify /path/to/checkout
```

This reproduces the `manifest_sha256` from the checkout's `.bernstein/volunteer.json` and compares it to the receipt. Mismatch indicates the project changed its policy after the work was submitted.
