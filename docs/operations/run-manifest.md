# Agent run manifest

Every `bernstein run` writes a manifest capturing the complete configuration
of that orchestration run: model routing, budget ceiling, approval-gate
mode, agent-adapter settings, and who/when/what-commit triggered it. The
configuration is written at run start; the final delegation chain head is
committed atomically at run close. The hash covers the whole declaration.

> This is a different concept from [named team manifests](team-manifests.md)
> (`core/teams/manifest.py`), which pin a reusable role/model/response-profile
> bundle referenced from `bernstein.yaml`. A run manifest is a one-shot,
> per-run configuration snapshot generated automatically.

## Which document answers what?

| Source | What it answers | Input |
|---|---|---|
| [Saved run manifest](#how-to-use-it) | What configuration and provenance were declared for this run? | The run's declaration, written at run start and finalized at close. |
| [Reusable team manifest](team-manifests.md) | What roles, model policies, response profiles and coordination settings define a named team? | A reusable, named team definition referenced from `bernstein.yaml`. |
| [Bill of materials](run-bom.md) | What model components are recorded as used by this run? | A projection of the run's recorded lineage spine, not the saved run declaration. |

The saved run manifest and lineage-derived bill of materials are both addressed
by run identifier, but are different run-specific views built from different
inputs: the declaration and the recorded lineage, respectively. See the
[AI bill-of-materials guide](run-bom.md) for details.

## How to use it

```
bernstein manifest list           # list every run with a saved manifest
bernstein manifest show <run-id>  # display one run's full configuration
bernstein manifest diff <a> <b>   # compare two runs' configurations
```

`show` renders provenance (triggered-by, timestamp, commit SHA), workflow
identity, agent-adapter settings, budget/approval configuration, model
routing, and the full orchestrator-config snapshot for the run. `diff`
lists only the top-level fields that differ between two runs' canonical
payloads.

## What the manifest binds

| Field | Meaning |
|---|---|
| `run_id` | Timestamp-based unique run identifier. |
| `workflow_definition_hash` / `workflow_name` | Governed workflow identity, if any (empty in adaptive mode). |
| `model_routing` | Default model plus allowed/denied provider lists. |
| `budget_ceiling_usd` | Maximum spend allowed for the run (`0` = unlimited). |
| `approval_gates` | Approval mode (`auto` / `review` / `pr`) and plan-mode flag. |
| `agent_adapter` | CLI adapter, model, max agents, max tasks per agent. |
| `provenance` | `triggered_by` (OS user), `triggered_at_iso`, `commit_sha` (`git rev-parse HEAD`). |
| `orchestrator_config` | Snapshot of the run's `OrchestratorConfig` fields (poll interval, retries, merge strategy, recovery mode, dry-run, plan mode, and more). |
| `delegation_head_hmac` / `delegation_hop_count` | Last delegation receipt HMAC and receipt count, declared at close; absent before close. |
| `delegation_seal` | Audit-key HMAC authenticating the final declaration and configuration. |
| `manifest_hash` | SHA-256 over the canonical JSON of every other field. |

## Hashing and finalization

`manifest_hash` is computed over the canonical (sorted-key, minimal-separator)
JSON serialization of the manifest with `manifest_hash` itself excluded and
unset delegation fields omitted (preserving pre-upgrade hashes), so
the hash identifies the configuration and, after close, the delegation head.
Configuration fields stay fixed. After writers drain, the orchestrator adds
the final receipt digest/count, seals the declaration with the audit key, and
recomputes `manifest_hash`, committing the result in one atomic replacement under the delegation append lock. A failed
write leaves the startup manifest intact. Older or unclosed manifests without
a head produce an incomplete delegation verification (exit `3`), never valid.
See [delegation verification](delegation-verify.md).

The SHA-256 digest is a checksum, not a signature. `delegation_seal` is an
audit-key HMAC over the manifest fields except the seal and public checksum.
Verification checks this seal as well as the head/count. A keyless writer cannot
forge a shorter declaration by recomputing the checksum. See
[delegation verification](delegation-verify.md) for export layout, legacy behavior
and replay/key-holder limits.

## Where it lives

```
.sdd/runtime/manifests/<run-id>.json
```

## Source

`src/bernstein/core/config/manifest.py` (`RunManifest`, `build_manifest`,
`diff_manifests`), `src/bernstein/cli/commands/manifest_cmd.py`
(`bernstein manifest`).
