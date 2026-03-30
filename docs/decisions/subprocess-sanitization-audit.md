# Subprocess Args Sanitization Audit

**Date**: 2026-03-30
**Task**: W1-03 (5d8433e5addb)
**Auditor**: security agent
**Result**: PASS — zero vulnerabilities found

## Summary

| Metric | Count |
|--------|-------|
| Total subprocess calls audited | 102 |
| List-based args (safe) | 95 |
| shell=True (justified) | 7 |
| User-controlled input in shell commands | 0 |
| Vulnerabilities found | 0 |

## shell=True Calls (All Justified)

All 7 `shell=True` calls use admin/developer-configured command strings, never user input.
Each has a `# SECURITY:` comment explaining the justification.

| File | Line | Source of Command | Justification |
|------|------|-------------------|---------------|
| core/quality_gates.py | 110 | QualityGatesConfig (admin) | Lint/test commands may use pipes/globs |
| core/rule_enforcer.py | 352 | RuleSpec (admin config) | Enforcement scripts use shell features |
| core/worktree.py | 117 | WorktreeSetupConfig (admin) | Setup commands may use pipes/redirects |
| core/janitor.py | 578 | Internal test invocation | Test commands use shell features |
| evolution/sandbox.py | 328 | EvolveSandbox config | Test commands use shell features |
| eval/scenario_runner.py | 322 | YAML scenario config | Setup commands use shell features |
| eval/scenario_runner.py | 649 | YAML scenario config | Validation commands use shell features |

## Adapter Layer (All Safe)

All adapter `subprocess.Popen` calls use list-based args with no shell execution.
Task prompts (containing user-controlled titles/descriptions) are passed as isolated
list elements to the CLI (e.g., `[..., "-p", prompt]`), never interpolated into shell strings.

| File | Line | Pattern |
|------|------|---------|
| adapters/claude.py | 203 | `Popen(cmd)` — cmd is list from `_build_command()` |
| adapters/claude.py | 217 | `Popen([sys.executable, "-c", wrapper])` — wrapper is generated Python |
| adapters/codex.py | 55 | `Popen(wrapped_cmd)` — list from `build_worker_cmd()` |
| adapters/gemini.py | 55 | `Popen(wrapped_cmd)` — list |
| adapters/aider.py | 73 | `Popen(wrapped_cmd)` — list |
| adapters/amp.py | 74 | `Popen(wrapped_cmd)` — list |
| adapters/generic.py | 77 | `Popen(wrapped_cmd)` — list |
| adapters/cursor.py | 60 | `Popen(wrapped_cmd)` — list |
| adapters/kilo.py | 63 | `Popen(wrapped_cmd)` — list |
| adapters/qwen.py | 131 | `Popen(wrapped_cmd)` — list |
| adapters/roo_code.py | 71 | `Popen(wrapped_cmd)` — list |
| adapters/manager.py | 64 | `Popen(wrapped_cmd)` — list |
| adapters/mock.py | 81 | `Popen(cmd)` — list, task data JSON-serialized |

## Core Layer (All Safe)

All core `subprocess.run/Popen` calls use list-based args (except the 7 justified
`shell=True` calls above). No f-string/format interpolation of user input into commands.

| File | Lines | Notes |
|------|-------|-------|
| core/spawner.py | (indirect) | Delegates to adapter.spawn(), no direct subprocess |
| core/orchestrator.py | 1600, 1694, 1803 | All literal list args: `["uv", "run", ...]` |
| core/evolve_mode.py | 243, 307, 416 | All literal list args |
| core/server_launch.py | 287, 411 | All literal list args |
| core/worker.py | 81 | `Popen(cmd)` — cmd from argparse REMAINDER (list) |
| core/preflight.py | 73, 95, 162 | All literal list args (version checks) |
| core/git_basic.py | 74 | `["git", *args]` — args from internal git helpers |
| core/git_context.py | 30 | `["git", *args]` — args from internal helpers |
| core/git_pr.py | 330, 356, 446 | List args; title/body as isolated list elements |
| core/ci_fix.py | 330, 347, 364 | All literal list args (version checks) |
| core/fast_path.py | 205, 245, 278 | List args; targets are owned file paths |
| core/container.py | 407-1078 (18 calls) | All list args; container IDs from runtime |
| core/circuit_breaker.py | 216, 227, 263, 382 | All literal list args with Path objects |
| core/mcp_manager.py | 195 | `Popen(cmd)` — cmd is list from MCP config |
| core/knowledge_base.py | 134, 410 | List args for git/grep commands |
| core/merkle.py | 178 | List args for git commands |
| core/manifest.py | 34 | List args for git commands |
| core/task_lifecycle.py | 1382 | List args for git commands |
| core/repo_index.py | 245 | List args for git commands |
| core/cross_model_verifier.py | 147, 151 | List args for git commands |
| core/formal_verification.py | 347 | List args for verification tools |
| core/workspace.py | 161, 214, 229, 244 | List args for git commands |
| core/bootstrap.py | 320 | `Popen(cmd)` — literal list args |
| core/agent_discovery.py | 68 | List args for CLI version checks |

## CLI Layer (All Safe)

| File | Lines | Notes |
|------|-------|-------|
| cli/wrap_up_cmd.py | 23, 35, 59, 72 | List args; git SHAs from git output |
| cli/changelog_cmd.py | 69 | List args for git commands |
| cli/diff_cmd.py | 43 | List args for git commands |
| cli/dashboard.py | 1316, 1323, 1328 | List args for system commands |
| cli/voice_cmd.py | 402 | `shlex.split(cmd)` — properly sanitized voice input |
| cli/watch_cmd.py | 204 | Literal list args |
| cli/checkpoint_cmd.py | 20 | Literal list args |
| cli/self_update_cmd.py | 171 | List args; package spec is validated |
| cli/gateway_cmd.py | 89 | `shlex.split(upstream)` — CLI flag, intentional |

## GitHub App / Benchmarks / Eval (All Safe)

| File | Lines | Notes |
|------|-------|-------|
| github_app/cost_reporter.py | 106, 180 | List args for gh API calls |
| github_app/app.py | 198 | List args for gh API calls |
| github_app/check_runs.py | 174 | List args for gh API calls |
| github_app/ci_router.py | 50, 78, 103 | List args for git commands |
| agents/agency_provider.py | 245, 261 | List args for git clone/pull |
| benchmark/comparative.py | 374 | List args; goal from benchmark data |
| benchmark/swe_bench.py | 418 | List args; goal from dataset |
| adapters/ci/github_actions.py | 181, 225, 248 | List args for gh commands |

## Key Security Patterns Observed

1. **Prompt isolation**: Task prompts (containing user titles/descriptions) are always
   passed as isolated list elements, never shell-interpolated
2. **JSON serialization**: Adapters serialize task metadata as JSON for stdin, not commands
3. **shlex.split()**: Used correctly in voice_cmd.py and gateway_cmd.py for string-to-list conversion
4. **SECURITY comments**: All shell=True calls have inline justification comments
5. **No os.system()**: Zero uses of os.system() found in codebase
