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
- The donor's resource limits (if running with `--limit`)

The derived profile is hashed and recorded in the receipt as `sandbox_sha256`.

### Stage 4: Execute task

The task body (issue description + comments) is never executed as shell code. The worker:

1. Checks out the repo at the issue's base commit
2. Applies the sandbox containment
3. Runs the manifest's `gates` (e.g. `["uv", "run", "pytest", "-q"]`)
4. Records stdout/stderr, wall-clock time, and exit code

If any gate fails, execution stops and a partial receipt is produced.

### Stage 5: Produce receipt

A signed receipt is written to `~/.cache/bernstein/volunteer/receipts/<run_id>.json`. The receipt contains:

| Field | Description |
|-------|-------------|
| `manifest_sha256` | Digest of the manifest that was loaded |
| `sandbox_sha256` | Digest of the derived sandbox profile |
| `issue_url` | The issue that was worked |
| `gates` | Commands that were run |
| `exit_codes` | Exit code per gate |
| `wall_clock_seconds` | Total elapsed time |
| `timestamp` | ISO 8601 UTC |
| `signature` | Ed25519 signature over the canonical receipt body |

## Refusal paths

A refusal receipt is produced instead of a success receipt when:

| Stage | Refusal reason |
|-------|----------------|
| 1 | Invalid URL or host not allowed |
| 2 | Manifest absent or inactive |
| 3 | Sandbox derivation fails (e.g. unknown `sandbox` type) |
| 4 | Task times out or is killed by sandbox |

Refusal receipts carry the same structure as success receipts but have `status: "refused"` and a `refusal_reason` field.

## Verifying a receipt

```bash
bernstein volunteer verify /path/to/checkout
```

This reproduces the `manifest_sha256` from the checkout's `.bernstein/volunteer.json` and compares it to the receipt. Mismatch indicates the project changed its policy after the work was submitted.
