# CLI agent adapters

One adapter per upstream coding-agent CLI (claude, codex, gemini, aider, goose, opencode, 40+ more):
a task prompt becomes a CLI invocation and results stream back. One module per tool.

## Key files

| File | Purpose |
|---|---|
| `base.py` | Base adapter: spawn, timeout, kill, lineage write boundary |
| `_contract.py` | Loads per-adapter YAML capability contracts and asserts them |
| `capability_profile.py` | Declarative adapter capability profiles and factory |
| `admission.py` | Admission gate: validates spawn request against run-level and operator policies |
| `env_isolation.py` | Per-task environment isolation: credential filtering, network policy, sandbox wiring |
| `skills_injector.py` | Copies sanitized skill markdown into the worktree at dispatch |
| `registry.py` | Central name → adapter class mapping |
| `use_cases.py` | Human-readable adapter use-case catalogue for `bernstein init` |
| `opencode.py` | OpenCode adapter; qualifies bare model IDs from the operator's jsonc config |
| `scanner.py` | Base scanner: normalised `ScanResult`/`Finding` schema and scan receipt |
| `scanner_registry.py` | Registry for scanner adapters (nmap, semgrep, grype, trivy, gitleaks, …) |
| `nmap.py` | nmap network-scan adapter: XML normalisation, port-rule policy check |
| `semgrep.py` | Semgrep SAST adapter: SARIF parse, ruleset digest |
| `acp_channel.py` | Agent Communication Protocol channel bridging into the orchestrator event loop |
| `canary.py` | Nightly conformance canary matrix over adapter contracts |
| `onboarding.py` | Interactive probe and capability discovery for new agent CLIs |
| `mock.py` | Zero-API-key demo agent; produces real completion evidence |

## Invariants

- Every adapter has a YAML contract in `tests/contract/contracts/`; drift is a hard fail (exit 2).
- Artifact writes go through the lineage-spine boundary in `base.py`; no adapter-local write paths.
- `admission.py` is the last gate before a process starts; never bypass it.
- Scanner adapters produce byte-stable `ScanResult` so scans are content-addressable.

## Testing

Per-adapter unit tests: `tests/unit/test_adapter_<name>.py` or `tests/unit/adapters/`.
Contract checks: `tests/contract/`. Live-binary conformance: opt-in via `--live`.

<!-- Reviewed 2026-09-11 against this subtree; the notes above still hold. -->
