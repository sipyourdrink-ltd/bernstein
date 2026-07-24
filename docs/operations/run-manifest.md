# Agent run manifest

Every `bernstein run` writes a manifest capturing the complete configuration
of that orchestration run: model routing, budget ceiling, approval-gate
mode, agent-adapter settings, and who/when/what-commit triggered it. The
manifest is immutable, written once at run start, and hashed so its
configuration can be cited as SOC 2 / ISO 27001 evidence.

> This is a different concept from [named team manifests](team-manifests.md)
> (`core/teams/manifest.py`), which pin a reusable role/model/response-profile
> bundle referenced from `bernstein.yaml`. A run manifest is a one-shot,
> per-run configuration snapshot generated automatically.

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
| `manifest_hash` | SHA-256 over the canonical JSON of every other field. |

## Hashing and immutability

`manifest_hash` is computed over the canonical (sorted-key, minimal-separator)
JSON serialization of the manifest with `manifest_hash` itself excluded, so
the hash uniquely identifies the run's configuration. The manifest is
written once at run start and never mutated afterward — a config drift
between two runs shows up as a `manifest diff`, never as an edit to an
existing manifest file.

## Where it lives

```
.sdd/runtime/manifests/<run-id>.json
```

## Source

`src/bernstein/core/config/manifest.py` (`RunManifest`, `build_manifest`,
`diff_manifests`), `src/bernstein/cli/commands/manifest_cmd.py`
(`bernstein manifest`).
