# Compatibility

This page describes practical compatibility boundaries for Bernstein integrations.

Last updated: 2026-04-13

---

## Runtime compatibility

- Python: project targets Python 3.12+.
- Task server/API: FastAPI-based local or remote server operation.
- CLI adapters: 18 CLI agent adapters (17 third-party + generic) in `src/bernstein/adapters/`.

### Supported CLI agent adapters

| Adapter | Provider | Structured Output | MCP |
|---------|----------|-------------------|-----|
| `claude` | Anthropic | JSON schema enforced | Yes |
| `codex` | OpenAI | JSON (`--json`) | No |
| `gemini` | Google | JSON (`--output-format json`) | No |
| `openai_agents` | OpenAI (Agents SDK v2) | JSONL event stream | Yes (Bernstein-bridged) |
| `aider` | Multi | No | No |
| `amp` | Sourcegraph | No | No |
| `qwen` | Multi | No | No |
| `ollama` | Local | No | No |
| `cody` | Sourcegraph | No | No |
| `cursor` | Cursor | No | Yes |
| `goose` | Block | No | No |
| `roo-code` | Multi | JSON (`--output-format json`) | No |
| `continue` | Multi | No | No |
| `opencode` | Multi | JSON (`--format json`) | No |
| `kiro` | AWS | No | No |
| `kilo` | Stackblitz | No | Yes (ACP/MCP) |
| `tabby` | Self-hosted | No | No |
| `iac` | N/A (Terraform/Pulumi) | No | No |
| `generic` | Any | Depends on CLI | No |

### Support modules

| Module | Purpose |
|--------|---------|
| `caching_adapter` | Prompt prefix deduplication and response reuse |
| `claude_agents` | Subagent definitions for Claude Code `--agents` flag |
| `claude_exit_codes` | Exit code to lifecycle enum mapping |
| `claude_stream_parser` | Stream-JSON event parsing |
| `conformance` | Golden-transcript replay and adapter conformance testing |
| `env_isolation` | Environment variable filtering for credential safety |
| `manager` | Internal ManagerAgent spawner |
| `plugin_sdk` | Third-party adapter plugin base classes |
| `registry` | Adapter discovery and registration |
| `skills_injector` | Per-task Claude Code skill injection |

Compatibility details can vary by adapter version and local toolchain.

---

## Protocol and integration layers

### MCP

- Bernstein includes an MCP server (`src/bernstein/core/protocols/mcp_server.py`) exposed via `bernstein mcp`.
- MCP tool registry with auto-discovery and per-task configuration.
- MCP gateway proxy (`bernstein gateway`) for routing MCP traffic.
- MCP health monitoring, lazy discovery, sandbox, marketplace, and metrics modules in `src/bernstein/core/protocols/`.
- MCP auth lifecycle management and version compatibility checking.
- MCP composition and skill bridge for combining tools across servers.
- Practical compatibility depends on client/runtime transport expectations.

### A2A

- A2A task/artifact routes implemented in task routes.
- A2A federation support (`a2a_federation.py`) for cross-instance agent coordination.
- A2A available as part of the server API surface.

### ACP

- ACP IDE bridge (`acp_ide_bridge.py`) for editor integration.
- ACP-related compatibility workflows and spec docs exist.
- Treat ACP support as integration-dependent rather than one fixed matrix.

### Protocol negotiation

- Runtime protocol version handshake (`protocol_negotiation.py`) for MCP/A2A/ACP.
- Schema registry (`schema_registry.py`) for versioned message schemas.
- Ensures protocol compatibility is detected at connection time, not at failure time.

---

## Quality gates

Bernstein v1.8.4 ships an expanded quality gate pipeline in `src/bernstein/core/quality/`:

- Standard gates: lint, type-check, tests, coverage
- Architecture conformance (`arch_conformance.py`, `arch_rules.py`)
- Benchmark gate (`benchmark_gate.py`, `perf_benchmark_gate.py`)
- Mutation testing (`mutation_testing.py`, `test_mutation_verify.py`)
- Dead code detection (`dead_code_detector.py`)
- Dependency scanning (`dependency_scan.py`, `dep_validator.py`)
- Flaky test detection (`flaky_detector.py`)
- Integration test generation (`integration_test_gen.py`)
- Cross-model verification (`cross_model_verifier.py`)
- Consensus verification (`consensus_verifier.py`)
- LLM judge (`llm_judge.py`)
- Gate caching (`gate_cache.py`) and plugin system (`gate_plugins.py`)

---

## Cost and quota management

- Peak-hour scheduling (`cost/peak_hour_router.py`) for time-of-day cost optimization
- Cost anomaly detection, forecasting, and root cause analysis
- Budget actions and completion budgets
- Cost arbitrage across providers
- Cloud cost export integration

---

## How to verify in your environment

Use environment-specific validation instead of relying on static matrices:

1. Run `bernstein doctor`.
2. Run your target CLI adapter smoke checks (`bernstein test-adapter <name>`).
3. Validate required API endpoints (`/status`, `/tasks`, `/metrics`, protocol-specific routes).
4. If using remote workers, validate cluster endpoints and auth paths.
5. Generate a debug bundle (`bernstein debug`) for comprehensive triage information.

---

## Notes on historical matrices

Older protocol matrices in docs/workflows are useful as references for prior CI checks, but they should not be treated as evergreen compatibility guarantees for all environments.
