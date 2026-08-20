# CLI agent adapters

One adapter per upstream coding-agent CLI (claude, codex, gemini,
aider, goose, and 40+ more). An adapter turns a task prompt into a CLI
invocation and streams results back; flat layout, one module per tool.

## Key files

| File | Purpose |
|---|---|
| `base.py` | Base adapter: spawn, timeout, and kill discipline; lineage write boundary |
| `_contract.py` | Loads the per-adapter YAML capability contracts and asserts them |
| `capability_profile.py` | Declarative adapter capability profiles and profile factory |
| `skills_injector.py` | Copies sanitized skill markdown into the worktree at dispatch |
| `canary.py` | Nightly conformance canary matrix over adapter contracts |
| `mock.py` | Mock agent for zero-API-key demos; produces the same completion evidence real agents do (`Modified:` log lines plus a per-fix commit scoped to the mutated file) |

## Invariants

- Every adapter has a YAML contract in `tests/contract/contracts/` naming
  its required flags/subcommands. Capability assertions only, never snapshot
  `--help` text (`_contract.py` docstring); drift is a hard fail (exit 2).
- Artifact writes go through the lineage-spine boundary in `base.py`;
  do not add adapter-local artifact write paths
  (`../core/lineage/spine.py`).
- Keep adapter module import time free of replay-journal imports;
  `base.py` duplicates capability constants for exactly this reason.
- Default spawned-process timeout is 30 minutes
  (`DEFAULT_TIMEOUT_SECONDS` in `base.py`); adapters get SIGTERM, then
  SIGKILL after a grace period.

## Testing

Per-adapter unit tests are mostly flat as `tests/unit/test_adapter_<name>.py`,
with a few under `tests/unit/adapters/` beside the shared subsystem tests.
Both layouts are current; follow whichever one an adapter already uses, and
run one file at a time. Contract checks live under `tests/contract/`;
live-binary conformance is opt-in via the `--live` pytest flag.

<!-- Reviewed 2026-08-18 against this subtree; the notes above still hold. -->
