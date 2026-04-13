# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

Release notes for tagged versions are also generated automatically by
[release-drafter](https://github.com/release-drafter/release-drafter) and
published on the [GitHub Releases page](https://github.com/chernistry/bernstein/releases).
This file captures the human-curated highlights.

## [Unreleased]

### Added
- Placeholder section for changes landing on `main` between releases.

## [1.7.0] - 2026-04-13

### Changed
- **Major architecture refactoring**: reorganized `core/` from 533 flat files into 22 sub-packages
  (orchestration/, agents/, tasks/, quality/, server/, cost/, tokens/, security/, config/,
  observability/, protocols/, git/, persistence/, planning/, routing/, communication/,
  knowledge/, plugins_core/, routes/, memory/, trigger_sources/, grpc_gen/).
- Module decomposition: `orchestrator.py` split into 7+ sub-modules, `spawner` into 4,
  `task_lifecycle` into 4, with backward-compatible shims at the original import paths.
- Created `defaults.py` with 150+ configurable constants extracted from scattered literals.
- CLI commands reorganized into `cli/commands/` sub-package (70+ command modules).

### Added
- `bernstein debug-bundle` command for collecting logs, config, and state for bug reports.
- IaC (Infrastructure-as-Code) adapter.
- 2,600+ new tests (total test files now exceed 1,000).
- Protocol negotiation for MCP/A2A compatibility.
- Quality gates, cost tracking, and token monitoring moved into dedicated sub-packages.

### Fixed
- Numerous orchestration, lifecycle, and merge-ordering bugs addressed during refactoring.

## [1.6.4] - 2026-04-11

### Fixed
- Orchestration: serialize merges via lock; remove dangerous pre-merge rebase.
- Spawner: close path-traversal and log-injection in retry path.
- File locks: add threading lock to `FileLockManager`; protect approval gate.
- Agents: reap agents before fetch; protect verify loop; FIFO eviction.
- Completion flow reordered — merge before close, cleanup after PR.
- 20 critical orchestration bugs covering merge serialization, gate ordering,
  completion flow, and agent lifecycle.
- GitHub sync skips issues that already have an assignee.
- CI: mutation testing score parser correctness.
- Activity-summary poller debounced flaky timing assertion.

## [1.6.0] - 2026-04
### Added
- CLI command aliases wired through the main entry point.

## [1.5.0] - 2026-03
### Added
- Multi-repo workspace commands and cluster mode improvements.

## [1.4.0] - 2026-02
### Added
- Knowledge graph for codebase impact analysis.
- Semantic caching to reduce token spend on repeated patterns.
- Cost anomaly detection with Z-score flagging.

## [1.3.0] - 2026-01
### Added
- Cross-model code review.
- HMAC-chained tamper-evident audit logs.
- WAL-based crash recovery.

## [1.2.0] - 2025-12
### Added
- Quality gates: lint + types + PII scan pipeline.
- Token growth monitoring with auto-intervention.

## [1.1.0] - 2025-11
### Added
- Janitor verification of concrete completion signals.
- Circuit breaker for misbehaving agents.

## [1.0.0] - 2025-10
### Added
- Initial public release.
- Deterministic Python orchestrator with file-based state in `.sdd/`.
- Adapters for Claude Code, Codex CLI, Gemini CLI, Cursor, Aider, and a generic
  `--prompt` adapter.
- YAML plan execution (`bernstein run plan.yaml`).
- TUI dashboard, web dashboard, Prometheus `/metrics`, and OTel exporter
  presets.

[Unreleased]: https://github.com/chernistry/bernstein/compare/v1.7.0...HEAD
[1.7.0]: https://github.com/chernistry/bernstein/compare/v1.6.4...v1.7.0
[1.6.4]: https://github.com/chernistry/bernstein/compare/v1.6.0...v1.6.4
[1.6.0]: https://github.com/chernistry/bernstein/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/chernistry/bernstein/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/chernistry/bernstein/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/chernistry/bernstein/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/chernistry/bernstein/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/chernistry/bernstein/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/chernistry/bernstein/releases/tag/v1.0.0
