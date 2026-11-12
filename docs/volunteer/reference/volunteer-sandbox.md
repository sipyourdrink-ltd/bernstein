# Volunteer sandbox profile

The sandbox profile records a containment decision derived from the project
manifest, the donor's limits and available backends. The same inputs produce
the same `sandbox_sha256`.

The current claimed-task runner starts a host process in a Git worktree with
a filtered environment and a wall-clock cap. The profile records the selected
backend and resource settings for callers that integrate a sandbox backend.

## Derivation

```
manifest.json  +  donor_limits  →  sandbox_profile  →  sandbox_sha256
```

Inputs to the derivation:
- `manifest.sandbox`
- `manifest.allowed_paths`
- `manifest.egress_allowlist`
- `manifest.max_wall_clock_minutes`
- Optional donor memory and wall-clock ceilings supplied through the runner API

The tighter ceiling applies. The effective egress allowlist includes the
manifest entries and the built-in package registries.

## Profile settings

| Setting | Profile value |
|---------|---------------|
| Backend | Selected from the available backends subject to the manifest and donor consent |
| Network | Effective egress allowlist |
| Memory | Ceiling of 2 GiB, lowered by an optional donor memory limit |
| CPU | Default quota supplied by the profile |
| Time | Lower of the manifest and optional donor wall-clock limits |

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
