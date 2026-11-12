# Volunteer sandbox profile

The sandbox profile is the containment boundary derived from the project's manifest and the donor's resource limits. It is a pure function — the same manifest always produces the same `sandbox_sha256` on any compliant donor.

## Derivation

```
manifest.json  +  donor_limits  →  sandbox_profile  →  sandbox_sha256
```

Inputs to the derivation:
- `manifest.sandbox`
- `manifest.allowed_paths`
- `manifest.egress_allowlist`
- `manifest.max_wall_clock_minutes`
- Donor flags: `--cpu`, `--memory`, `--no-network`

The donor's resource flags override the manifest's implicit defaults but cannot loosen restrictions (e.g. a manifest with `egress_allowlist: []` blocks all egress regardless of donor flags).

## Sandbox types

### `container` (rootless)

Linux namespace isolation using `unshare`. The worker runs as a non-root user inside a new PID, mount, network, and IPC namespace. No capabilities are retained.

| Resource | Restriction |
|----------|-------------|
| Filesystem | chroot to repo root; read/write limited to `allowed_paths` globs |
| Network | New namespace; no host network unless `egress_allowlist` is populated |
| PIDs | New PID namespace; max processes limited by donor `--pids` flag |
| Memory | Limited by donor `--memory` flag (default 2 GiB) |
| CPU | Cgroup v2 shares limited by donor `--cpu` flag (default 2 cores) |
| Time | `max_wall_clock_minutes` enforced via `RLIMIT_CPU` |

### `none`

No containment applied. The task runs in the donor's ordinary environment. Only `allowed_paths` file restrictions apply.

> **Warning:** `sandbox: "none"` is only appropriate for fully trusted communities. Use only with `local_ok: true` in the manifest.

## Receipt fields

The produced receipt includes:

| Field | Description |
|-------|-------------|
| `sandbox_sha256` | Digest of the derived sandbox profile |
| `sandbox_type` | `"container"` or `"none"` |
| `wall_clock_seconds` | Observed elapsed time |
| `exit_codes` | Exit code per gate command |

## Verifying containment

A receipt's `sandbox_sha256` can be independently recomputed by:
1. Loading the manifest from the checkout
2. Applying the same donor flags
3. Hashing the canonical profile JSON

Mismatches indicate the donor used a non-compliant sandbox or the manifest changed between submission and verification.
