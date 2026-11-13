# Volunteer sandbox profile

The sandbox profile records a containment decision derived from the project
manifest, the donor's limits and available backends. The same inputs produce
the same profile digest.

The current claimed-task runner starts a host process in a Git worktree with
a filtered environment and a wall-clock cap. The profile records the selected
backend and resource settings for callers that integrate a sandbox backend.

## Derivation

```
manifest + donor limits + backend availability and consent → profile → digest
```

Inputs to the derivation:
- `manifest.sandbox`
- `manifest.allowed_paths`
- `manifest.egress_allowlist`
- `manifest.max_wall_clock_minutes`
- Available backends and donor consent for plain-container execution
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

## Result bundle

The signed result bundle records the profile digest in `sandbox_profile`.
To recompute the digest, load the manifest at the attested base commit and
derive the profile with the same backend and donor inputs. The digest binds
the declared profile. Runtime enforcement depends on the caller.
