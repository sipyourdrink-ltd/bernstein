# CLI agent adapters

One adapter per upstream coding-agent CLI (claude, codex, gemini, aider, goose, 50+ more): a task prompt becomes a CLI invocation and results stream back. One module per tool. The subtree also holds the scanner contract, deep-research adapters, and ingest adapters for runtimes Bernstein did not schedule.

## Key files

| File | Purpose |
|---|---|
| `base.py` | Base adapter: spawn, timeout, and kill discipline; lineage write boundary |
| `registry.py` | `_ADAPTERS` name -> class map, plus adapters built from capability profiles |
| `_contract.py` | Loads the per-adapter YAML capability contracts and asserts them |
| `capability_profile.py`, `draft.py` | Declarative capability profiles; `draft.py` drafts one from probe evidence during onboarding |
| `skills_injector.py` | Copies sanitized skill markdown into the worktree at dispatch |
| `canary.py` | Nightly conformance canary matrix over adapter contracts |
| `goose_stream_parser.py` | Parses Goose `--output-format stream-json`; token accounting rides the terminal `envelope` event |
| `http_429_classifier.py` | Classifies HTTP 429 errors into standing quotas vs. transient rate limits |
| `onboarding.py` | Interactive probe and capability discovery for newly installed agent CLIs |
| `mock.py` | Mock agent for zero-API-key demos; produces the same completion evidence real agents do (`Modified:` log lines plus a per-fix commit scoped to the mutated file) |
| `scanner.py`, `scanner_registry.py`, `scanner_conformance.py` | `ScannerAdapter` contract for external analysis tools (semgrep, gitleaks, trivy, nmap), its registry, and golden-transcript conformance |
| `deep_research.py` | Shared machinery for `gpt_researcher.py` and `tongyi_deepresearch.py`: per-agent gateway variables, egress check, standalone runner, digest-checked read-back |
| `crew_ingest.py`, `directory/`, `digest/` | Ingest of role-and-crew traces as delegation receipts; SCIM directory provisioning; deterministic tool-output digesters |

## Invariants

- An adapter's YAML contract in `tests/contract/contracts/` names its required flags/subcommands. Capability assertions only, never snapshot `--help` text (`_contract.py` docstring); drift is a hard fail (exit 2). Not every registered adapter has a contract yet; a new adapter should ship one.
- Artifact writes go through the lineage-spine boundary in `base.py`; do not add adapter-local artifact write paths (`../core/lineage/spine.py`).
- Keep adapter module import time free of replay-journal imports; `base.py` duplicates capability constants for exactly this reason.
- Default spawned-process timeout is 30 minutes (`DEFAULT_TIMEOUT_SECONDS` in `base.py`); adapters get SIGTERM, then SIGKILL after a grace period.
- A scanner's `determinism` tier (`deterministic`, `feed_pinned`, `transcript_anchored`) is what the conformance suite demands of it, not documentation (`scanner.py`).
- A deep-research agent reads only its own `BERNSTEIN_<AGENT>_*` gateway key, runs in its own interpreter, and is refused before spawn when the network policy denies its hosts. A report whose digests no longer match reads back as `TAMPERED` (`deep_research.py`).

## Testing

Per-adapter unit tests are mostly flat as `tests/unit/test_adapter_<name>.py`, with a growing set under `tests/unit/adapters/` beside the shared subsystem tests. Both layouts are current; follow whichever one an adapter already uses, and run one file at a time. Contract checks live under `tests/contract/`; live-binary conformance is opt-in via the `--live` pytest flag.

<!-- Reviewed 2026-10-16 against this subtree; the notes above still hold. -->
