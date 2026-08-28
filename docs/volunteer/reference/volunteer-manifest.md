# Volunteer project manifest

The manifest `.bernstein/volunteer.json` declares a project's volunteer participation policy. A project commits this file to opt in; donors read it to derive containment boundaries.

## Schema

```json
{
  "version": 1,
  "license": "Apache-2.0",
  "gates": [["uv", "run", "pytest", "-q"]],
  "allowed_paths": ["src/**", "tests/**", "docs/**"],
  "egress_allowlist": [],
  "sandbox": "container",
  "max_wall_clock_minutes": 30,
  "task_label": "volunteer-ok",
  "local_ok": true,
  "status": "active"
}
```

## Field reference

### `version` (required)

Integer. Must be `1`. Higher versions are rejected with a refusal receipt.

### `license` (required)

SPDX license identifier. The volunteer waives any copyright claim on work produced under this license. Use `"Apache-2.0"` or `"MIT"` for permissive licensing.

### `gates` (required)

Array of arrays of strings. Each inner array is a command run via `sh -c`. All commands must exit 0 for the task to succeed.

| Field | Type | Description |
|-------|------|-------------|
| `gates` | `string[][]` | Ordered list of gate commands |

**Example:**
```json
"gates": [
  ["uv", "run", "pytest", "-q"],
  ["uv", "run", "ruff", "check", "."]
]
```

### `allowed_paths` (required)

Array of glob patterns relative to the repo root. Files outside these patterns cannot be read or written during task execution.

**Example:**
```json
"allowed_paths": ["src/**", "tests/**", "docs/**"]
```

### `egress_allowlist` (optional)

Array of hostnames. If empty, all outbound network access is blocked. If populated, only these hosts may be contacted.

**Example:**
```json
"egress_allowlist": ["pypi.org", "github.com"]
```

### `sandbox` (required)

String enum. The containment technology to use.

| Value | Description |
|-------|-------------|
| `"container"` | Linux namespace container (rootless) |
| `"none"` | No containment (use only with `local_ok: true` and trusted donors) |

### `max_wall_clock_minutes` (required)

Integer. Hard limit on task execution time. The worker sends SIGKILL if this is exceeded.

### `task_label` (required)

GitHub issue label that marks issues available for volunteers. The donor's browser command filters by this label.

### `local_ok` (optional)

Boolean. If `true`, donors may run tasks on their local machine (bypassing the sandbox). Defaults to `false`. Setting to `true` without a matching `sandbox: "none"` has no effect.

### `status` (required)

String enum.

| Value | Description |
|-------|-------------|
| `"active"` | The project accepts volunteer submissions |
| `"paused"` | No new submissions; existing receipts still verifiable |

## Verification

Run `bernstein volunteer verify` in a repo checkout to validate the manifest and print its `manifest_sha256`:

```bash
bernstein volunteer verify
# manifest_sha256: abc123...
```

The digest is stable across formatting changes (canonical JSON, sorted keys).
