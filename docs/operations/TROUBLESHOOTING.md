# Troubleshooting Guide

Comprehensive reference for diagnosing and resolving Bernstein failures.
Each entry follows the pattern: symptom, cause, diagnosis, resolution.

**Jump to a failure mode:**
1. [Adapter Not Found](#1-adapter-not-found)
2. [CLI Binary Not in PATH](#2-cli-binary-not-in-path)
3. [API Key Not Set](#3-api-key-not-set)
4. [Rate Limiting / Provider Throttling](#4-rate-limiting-provider-throttling)
5. [Budget Exceeded](#5-budget-exceeded)
6. [Permission Denied on Agent Spawn](#6-permission-denied-on-agent-spawn)
7. [Task Server Unreachable](#7-task-server-unreachable)
8. [Worktree Corruption](#8-worktree-corruption)
9. [Merge Conflicts After Agent Completion](#9-merge-conflicts-after-agent-completion)
10. [Config File Errors (bernstein.yaml)](#10-config-file-errors-bernsteinyaml)
11. [MCP Server Connection Failures](#11-mcp-server-connection-failures)
12. [Agent Timeout (Watchdog Kill)](#12-agent-timeout-watchdog-kill)
13. [Orphaned Tasks (Agent Crash Mid-Task)](#13-orphaned-tasks-agent-crash-mid-task)
14. [Dependency Deadlock (Blocked Tasks)](#14-dependency-deadlock-blocked-tasks)
15. [Spawn Failure (Fast Exit Detection)](#15-spawn-failure-fast-exit-detection)
16. [Stale PID Files](#16-stale-pid-files)
17. [JSONL State File Corruption](#17-jsonl-state-file-corruption)
18. [Template Rendering Failure](#18-template-rendering-failure)
19. [Container Isolation Failures](#19-container-isolation-failures)
20. [Plan YAML Parse Errors](#20-plan-yaml-parse-errors)

---

## 1. Adapter Not Found

**Symptom:** `ValueError: Unknown adapter 'foo'. Available: aider, amp, claude, ...`

**Cause:** The `cli` field in `bernstein.yaml` or the `--cli` flag specifies an adapter name that is not registered. Typos are the most common cause.

**Diagnosis:**
```bash
bernstein agents   # lists all registered adapters
```
Check your `bernstein.yaml`:
```yaml
cli: claude   # must match a registered name exactly
```

**Resolution:**
- Use one of the built-in names: `aider`, `amp`, `claude`, `codex`, `cody`, `continue`, `cursor`, `gemini`, `generic`, `goose`, `iac`, `kilo`, `kiro`, `mock`, `ollama`, `opencode`, `qwen`, `roo-code`, `tabby`.
- For third-party adapters, ensure the package exposes a `bernstein.adapters` entry point and is installed in the same virtualenv.
- For arbitrary CLIs, use `cli: generic` with `cli_command`, `prompt_flag`, and `model_flag` settings.

---

## 2. CLI Binary Not in PATH

**Symptom:** `RuntimeError: claude not found in PATH` (or `codex`, `gemini`, `aider`, etc.)

**Cause:** The adapter tries to `subprocess.Popen(["claude", ...])` and the binary does not exist on `$PATH`.

**Diagnosis:**
```bash
which claude      # should print a path
claude --version  # should print version info
```

**Resolution:**
- Install the CLI: `npm install -g @anthropic-ai/claude-code` (Claude), `npm install -g @openai/codex` (Codex), `pip install aider-chat` (Aider), etc.
- If installed but not on PATH, add its directory: `export PATH="$HOME/.npm-global/bin:$PATH"`.
- If running inside a virtualenv or container, ensure the binary is available in that environment.

### 2a. Windows: `.cmd`/`.bat` shim not found (nvm-windows)

**Symptom:** `bernstein-worker: command not found: codex` (exit 127) on Windows, even though `where.exe codex` and `python -c "import shutil; print(shutil.which('codex'))"` both resolve `C:\...\codex.cmd`.

**Cause:** Node version managers such as nvm-windows install the Codex/Claude/Gemini CLIs as batch shims (`codex.cmd`), not real `.exe` files. `CreateProcess` (what `subprocess.Popen` calls without a shell) does not consult `PATHEXT` for `argv[0]`, so a bare `codex` never resolves the `.cmd`, and even a full path to a `.cmd` cannot be launched directly.

**Resolution:** Bernstein now resolves the bare name via `shutil.which` and, on Windows, routes a resolved `.cmd`/`.bat` shim through `cmd.exe /c` automatically. Ensure `PATHEXT` is present in your environment (it is passed through to spawned agents). No manual action is needed on current versions; if you see this on an older build, upgrade Bernstein.

---

## 3. API Key Not Set

**Symptom:** Adapter logs a warning like `ClaudeCodeAdapter: ANTHROPIC_API_KEY is not set` and the spawned process exits immediately with a non-zero code.

**Cause:** Each adapter requires provider-specific environment variables. The adapter forwards only allow-listed keys via `build_filtered_env()`.

**Diagnosis:**
```bash
# Check which keys are set:
env | grep -E 'ANTHROPIC|OPENAI|GOOGLE|GEMINI'
```

**Resolution:**
| Adapter | Required env vars |
|---------|-------------------|
| `claude` | `ANTHROPIC_API_KEY` |
| `codex` | `OPENAI_API_KEY` |
| `gemini` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` |
| `aider` | `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` |
| `amp` | `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` (+optional `SRC_ENDPOINT`, `SRC_ACCESS_TOKEN`) |
| `qwen` | Depends on provider: `OPENROUTER_API_KEY_PAID`, `TOGETHERAI_USER_KEY`, etc. |
| `cody` | `SRC_ACCESS_TOKEN`, `SRC_ENDPOINT` |
| `ollama` | None (local), but needs `aider` and `ollama` installed |
| `opencode` | Provider-specific: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc. |
| `kiro` | `KIRO_API_KEY` or `~/.kiro` auth |
| `kilo` | `KILO_API_KEY` or `~/.kilo/` OAuth |
| `tabby` | `TABBY_SERVER_URL` (default: `http://127.0.0.1:8080`) |
| `cursor` | OAuth via `~/.cursor/` (no env var needed) |

Set them in your shell profile or a `.env` file loaded before `bernstein run`.

---

## 4. Rate Limiting / Provider Throttling

**Symptom:** Agents exit immediately. Logs contain `"rate limit"`, `"429"`, `"you've hit your limit"`, or `"quota exceeded"`. The orchestrator raises `RateLimitError`.

**Cause:** The API provider is throttling requests. Claude Code is particularly aggressive about account-level limits. The `ClaudeCodeAdapter.is_rate_limited()` probes for this and blocks spawns for 5 minutes when detected.

**Diagnosis:**
```bash
# Check agent log for rate-limit signals:
grep -i "rate limit\|429\|hit your limit\|resets" .sdd/runtime/*.log
```

**Resolution:**
- Wait for the cooldown. The adapter auto-blocks spawns for `_RATE_LIMIT_COOLDOWN` (300s).
- Reduce concurrency: lower `max_agents` in `bernstein.yaml`.
- Switch to a higher-tier plan or add a second provider.
- Use the `TierAwareRouter` to balance across multiple providers (Claude + Codex + Gemini).
- For batch-eligible tasks, set `batch_eligible: true` to route to provider batch APIs at reduced cost.

### 4a. Healthy worker killed as a false-positive rate-limit hit

**Symptom:** A worker that was making normal progress gets killed and the
run falls back to a more expensive sticky provider, even though the
provider dashboard shows no throttling. `.sdd/runtime/*.log` shows a
`_scan_log_for_patterns: matched RISKY pattern ...` line pointing at a
structured `tool_call`/`tool_result` JSON line, a `"max_tokens": ...`
manifest dump, or a `"timeout": <n>` tool argument.

**Cause:** `core/observability/rate_limit_tracker.py`'s log-scanning
classifier (`_scan_log_for_patterns`) infers 429/throttle events from raw
agent subprocess output. Bare substrings (`"413"`, `"429"`, `"401"`,
`"403"`, `"504"`) and generic English words (`timeout`, `rate limit`,
`unauthorized`, ...) previously matched anywhere in the log tail,
including inside an agent's own JSON tool traffic where those tokens
carry zero relation to a real provider error (byte counts, arg names,
target-repo code being read). Fixed 2026-07-02 (#2183): structured
`tool_call`/`tool_result`/`heartbeat` lines are now skipped entirely, and
every remaining risky bare token/word only counts as a match when it
appears on a line that also carries explicit error context
(`error|status|http|code|exception|failed|failure|traceback`, matched
with a word boundary via `_ERROR_CONTEXT_RE`).

**Diagnosis:**
```bash
# Every classifier match is logged with the matched line - one read
# tells you whether it was a real provider error or a data false-positive.
grep "_scan_log_for_patterns: matched" .sdd/runtime/*.log
```

**Resolution:**
- If the matched line is agent-produced data (tool JSON, code being
  read) rather than a real HTTP/provider error, it's a classifier gap -
  file an issue with the matched line so the pattern/context list can be
  tightened further.
- No operator action needed for the fixed patterns above; upgrade to a
  release containing #2183 if you're still seeing this on unrelated data
  lines.

---

## 5. Budget Exceeded

**Symptom:** Tasks stop being scheduled. Logs show `"budget exceeded"` or the orchestrator enters read-only mode.

**Cause:** The `budget_usd` cap in `bernstein.yaml` has been hit. The `RunCostProjection` model projects costs and the orchestrator halts spending before the cap.

**Diagnosis:**
```bash
bernstein cost                     # shows spend breakdown
bernstein status                   # shows current spend vs budget
cat .sdd/runtime/cost_report.json
```

**Resolution:**
- Increase `budget_usd` in `bernstein.yaml`.
- Use cheaper models for low-complexity tasks: the router maps `Complexity.LOW` tasks to `haiku` automatically (see `PLAN.model_by_complexity` in `src/bernstein/core/defaults.py`).
- Enable `batch_eligible` for non-urgent tasks (approximately 50% cost reduction via batch APIs).
- Review per-agent cost with `bernstein cost --by model` and kill runaway agents.
- Use peak-hour routing (`core/cost/peak_hour_router.py`) to shift non-urgent work to off-peak windows.

---

## 6. Permission Denied on Agent Spawn

**Symptom:** `RuntimeError: Permission denied executing claude: [Errno 13]`

**Cause:** The CLI binary exists but is not executable, or the workdir/log directory is not writable.

**Diagnosis:**
```bash
ls -la $(which claude)
ls -la .sdd/runtime/
```

**Resolution:**
- Fix binary permissions: `chmod +x $(which claude)`.
- Fix directory permissions: `chmod -R u+w .sdd/`.
- If running as a different user (e.g., in CI), ensure the user has write access to the project directory.

---

## 7. Task Server Unreachable

**Symptom:** `httpx.ConnectError: Connection refused` or agents cannot claim tasks. The orchestrator reports `TaskStoreUnavailable`.

**Cause:** The task server (default `http://127.0.0.1:8052`) is not running, crashed, or is bound to a different port.

**Diagnosis:**
```bash
curl -s http://127.0.0.1:8052/status | python3 -m json.tool
lsof -i :8052   # check what is listening
```

**Resolution:**
- Start the server: `bernstein run` starts it automatically; for standalone use, check `src/bernstein/core/server/server_launch.py`.
- If the port is in use, set a different port in `bernstein.yaml` or via `BERNSTEIN_PORT`.
- Check firewall rules if running server and agents on different machines.
- If the server crashed, check `.sdd/runtime/server.log` for the stack trace.

---

## 8. Worktree Corruption

**Symptom:** `WorktreeError: failed to create worktree` or `fatal: 'path' is already registered as a worktree`.

**Cause:** A previous agent crashed without cleaning up its git worktree. Git worktrees have hard references in `.git/worktrees/` that survive process death.

**Diagnosis:**
```bash
git worktree list          # shows all worktrees
ls .git/worktrees/         # stale entries may exist here
ls .sdd/worktrees/         # bernstein worktrees live here
```

**Resolution:**
```bash
git worktree prune         # removes stale worktree entries
# If a directory still exists but git lost track:
git worktree remove <path> --force
```
The orchestrator's janitor attempts this automatically via `_save_partial_work()` before worktree cleanup, but manual intervention is needed when the `.git/worktrees/` metadata is inconsistent.

---

## 9. Merge Conflicts After Agent Completion

**Symptom:** Agent task completes (`DONE`) but merge fails. Log shows `MergeResult` with conflicts. Task may transition to `FAILED` or remain `DONE` without reaching `CLOSED`.

**Cause:** Two agents edited the same files concurrently. The `file_ownership` system and `check_file_overlap()` try to prevent this, but path inference from task titles is heuristic.

**Diagnosis:**
```bash
# Check the agent's branch:
git log --oneline agent/<session-id>
git diff main...agent/<session-id>
```

**Resolution:**
- The orchestrator will retry merges after other agents finish.
- Manually resolve: `git checkout agent/<branch> && git rebase main` then `git checkout main && git merge agent/<branch>`.
- Prevent future conflicts by adding explicit `owned_files` to tasks in plan YAML files.
- Increase the `depends_on` declarations between tasks that touch the same code.

**Injected skill files (`.claude/skills/bernstein-*.md`) causing conflicts on
*every* worker merge:** See [issue #2187](https://github.com/sipyourdrink-ltd/bernstein/issues/2187).
Each worker's worktree gets its own rendered copy of the always-injected
skills (`bernstein-completion-protocol.md`, `bernstein-signal-check.md`, plus
role-specific skills), with session-specific content
(`{{SESSION_ID}}`/`{{TASK_IDS}}`). If an agent's `git add -A` stages these
files, every worker merge conflicts on them - not because of real work
overlap, but because two agents' injected boilerplate collides. As of this
fix, `inject_skills()` registers each injected path in the target repo's
`.git/info/exclude` (resolved correctly for worktrees via
`git rev-parse --git-path info/exclude`), so the skills stay readable by
Claude Code but are never staged or committed. If you still see this on an
older Bernstein version, manually run `git rm --cached .claude/skills/bernstein-*.md`
and add the paths to `.git/info/exclude` to unblock merges.

---

## 10. Config File Errors (bernstein.yaml)

**Symptom:** `ValidationError` on startup, or unexpected default values being used.

**Cause:** Missing required fields, invalid YAML syntax, or type mismatches in `bernstein.yaml`.

**Diagnosis:**
```bash
python3 -c "import yaml; yaml.safe_load(open('bernstein.yaml'))"
```

**Resolution:**
- Check YAML syntax (indentation, colons, quotes).
- Required top-level fields: `cli`, `roles`. Optional: `budget_usd`, `max_agents`, `model`, `effort`.
- Boolean values must be `true`/`false` (not `yes`/`no` in strict mode).
- Refer to `CONFIG.md` for the full schema.

---

## 11. MCP Server Connection Failures

**Symptom:** Agent spawns but cannot connect to MCP servers. Log shows MCP handshake timeouts or `"connection refused"` for MCP endpoints.

**Cause:** The MCP server definition in `~/.claude/mcp.json` or `bernstein.yaml` `mcp_servers` points to a server that is not running, has a bad URL, or requires env vars that are not set.

**Diagnosis:**
```bash
cat ~/.claude/mcp.json                    # global MCP config
grep mcp_servers bernstein.yaml           # project-level overrides
```

**Resolution:**
- Start the MCP server before running Bernstein.
- Check that `${VAR}` references in MCP server configs resolve to actual env vars (the adapter calls `_resolve_env_vars()` recursively).
- Not all adapters support runtime MCP injection. Claude, Cursor, and Kilo support `--mcp-config`/`--add-mcp`/`--mcp` flags; others (Kiro, OpenCode, Continue, Tabby, Cody) ignore the `mcp_config` parameter.

---

## 12. Agent Timeout (Watchdog Kill)

**Symptom:** Agent is killed mid-task. Log shows `"Timeout after Xds: pid=... - sending SIGTERM"`.

**Cause:** The agent exceeded its wall-clock timeout. Default is 1800s (30 min). Scope-based timeouts: small=15min, medium=30min, large=60min. XL roles (architect, security, manager) get 120min. Adaptive timeouts (`core/orchestration/adaptive_timeout.py`) can adjust these based on historical task durations. All timeout constants are in `src/bernstein/core/defaults.py` (`TASK.scope_timeout_s`, `TASK.xl_timeout_s`).

**Diagnosis:**
```bash
grep -i "timeout" .sdd/runtime/<session>.log
```

**Resolution:**
- Increase `timeout_seconds` per-agent or in the orchestrator config.
- Break large tasks into smaller scopes (small/medium).
- The watchdog sends SIGTERM first, waits 30s for graceful shutdown, then SIGKILL. Partial work is committed via `_save_partial_work()` before worktree cleanup.
- If agents consistently time out, the task scope or complexity rating may be wrong.

---

## 13. Orphaned Tasks (Agent Crash Mid-Task)

**Symptom:** Task is stuck in `CLAIMED` or `IN_PROGRESS` with no live agent. Eventually transitions to `ORPHANED`.

**Cause:** The agent process died (OOM, SIGKILL, machine reboot) without reporting completion. The heartbeat monitor detects this and marks the task `ORPHANED`.

**Diagnosis:**
```bash
bernstein status                          # shows orphaned tasks
curl -s http://127.0.0.1:8052/tasks?status=orphaned
```

**Resolution:**
- The orchestrator auto-recovers orphaned tasks: `ORPHANED -> OPEN` (requeue) or `ORPHANED -> DONE` (if partial work is mergeable).
- If auto-recovery fails, manually requeue: `curl -X POST http://127.0.0.1:8052/tasks/<id>/reopen`.
- Check if the agent was OOM-killed (exit code 137): reduce concurrent agents or increase system memory.

---

## 14. Dependency Deadlock (Blocked Tasks)

**Symptom:** Tasks remain in `BLOCKED` state indefinitely. No agents are spawned for them.

**Cause:** Circular or unresolvable `depends_on` references between tasks. Task A depends on B, B depends on A (or a longer cycle).

**Diagnosis:**
```bash
curl -s http://127.0.0.1:8052/tasks?status=blocked | python3 -m json.tool
# Check depends_on fields for cycles
```

**Resolution:**
- Remove circular dependencies in your plan YAML.
- The task store validates dependencies on creation and raises HTTP 422 for cyclic deps, but runtime dependency changes can still deadlock.
- Manually unblock: transition the blocking task to `CANCELLED` or `DONE`.

---

## 15. Spawn Failure (Fast Exit Detection)

**Symptom:** `SpawnError: agent exited within 2s` immediately after spawn.

**Cause:** The adapter's `_probe_fast_exit()` detects that the process died before it could start working. Common causes: missing API key, invalid model name, CLI crash on startup.

**Diagnosis:**
```bash
cat .sdd/runtime/<session>.log   # usually contains the CLI's error message
```

**Resolution:**
- Read the log file. The CLI's stderr is captured there.
- Common fixes: set API key, fix model name (check `_MODEL_MAP` in the adapter), install missing dependencies.
- If the CLI requires interactive setup (e.g., `opencode auth login`), run it manually first.

---

## 16. Stale PID Files

**Symptom:** `bernstein ps` shows agents that are not actually running. New agents fail to spawn because Bernstein thinks slots are full.

**Cause:** PID metadata files in `.sdd/runtime/pids/` were not cleaned up after agent exit. This happens when the orchestrator is killed with SIGKILL (no cleanup hook).

**Diagnosis:**
```bash
ls .sdd/runtime/pids/
# For each PID file, check if process is alive:
cat .sdd/runtime/pids/<file>.json | python3 -c "import sys,json; print(json.load(sys.stdin)['pid'])"
```

**Resolution:**
```bash
rm .sdd/runtime/pids/*.json   # safe to delete; they are recreated on spawn
```
The `bernstein stop` command cleans these up, but hard kills (SIGKILL, power loss) leave them behind.

---

## 17. JSONL State File Corruption

**Symptom:** Task server fails to start with a JSON parse error. A JSONL state file (for example `.sdd/metrics/tasks.jsonl`) contains incomplete lines, or the shared JSON backlog `.sdd/runtime/task-backlog.json` fails to parse.

**Cause:** JSONL state files are persisted via append writes. If the process is killed mid-write, the last line may be truncated.

**Diagnosis:**
```bash
python3 -c "
import json
for i, line in enumerate(open('.sdd/metrics/tasks.jsonl'), 1):
    try: json.loads(line)
    except: print(f'Bad line {i}: {line[:80]!r}')
"
```

**Resolution:**
- Delete the corrupted last line(s). The JSONL format is append-only, so all prior lines are valid.
- For `.sdd/runtime/access.jsonl` (the request log), truncation is safe since it is append-only and non-authoritative.
- Consider switching to the PostgreSQL backend (`src/bernstein/core/persistence/store_postgres.py`) for production deployments.

---

## 18. Template Rendering Failure

**Symptom:** `TemplateError` when spawning an agent. The role prompt cannot be rendered.

**Cause:** The `templates/roles/<role>.md` file (or directory) is missing, contains invalid Jinja2 syntax, or references undefined variables.

**Diagnosis:**
```bash
ls templates/roles/           # check role file/directory exists
python3 -c "from bernstein.templates.renderer import render_role_prompt; print(render_role_prompt('backend', {}))"
```

**Resolution:**
- Ensure the role name in the task matches a directory in `templates/roles/`. Each role directory contains `system_prompt.md`, `task_prompt.md`, and `config.yaml`.
- Check Jinja2 syntax in the template (unclosed `{% %}` blocks, undefined `{{ var }}`).
- Custom roles need a corresponding template directory with the required files.

---

## 19. Container Isolation Failures

**Symptom:** `ContainerError` when spawning with `isolation: container`.

**Cause:** Docker/Podman is not running, the agent image does not exist, or resource limits are too restrictive.

**Diagnosis:**
```bash
docker ps                         # is Docker running?
docker images | grep bernstein    # does the agent image exist?
```

**Resolution:**
- Start Docker: `docker info` should succeed.
- Build the image: set `auto_build_image: true` in `ContainerIsolationConfig` or build manually.
- Increase `memory_mb` and `cpu_cores` if agents are being OOM-killed inside containers.
- For two-phase sandbox (Codex-style), Phase 1 needs network for deps, Phase 2 runs network-disabled.

---

## 20. Plan YAML Parse Errors

**Symptom:** `bernstein run plans/my-project.yaml` fails with a parse error or silently produces no tasks.

**Cause:** The plan YAML structure does not match the expected schema. Stages need `steps`, steps need `goal` and `role`.

**Diagnosis:**
```bash
python3 -c "
import yaml
plan = yaml.safe_load(open('plans/my-project.yaml'))
print(yaml.dump(plan, default_flow_style=False))
"
```

**Resolution:**
- Required plan structure:
  ```yaml
  stages:
    - name: stage_name
      steps:
        - goal: "What to do"
          role: backend
          priority: 2
          scope: medium
          complexity: medium
  ```
- Stage-level `depends_on: [other_stage]` must reference existing stage names.
- Step fields: `goal` (required), `role` (required), `priority`, `scope`, `complexity` (optional with defaults).
- Validate with `python3 -c "from bernstein.core.planning.plan_loader import load_plan; load_plan('plans/my-project.yaml')"`.

---

## 21. Janitor `path_exists` Fails on a Correctly-Placed File (Acceptance-Path Mismatch)

**Symptom:** A task's actual code/test changes are correct and merge cleanly, but the janitor still fails it with `path_exists: <some/guessed/path>`, the task retries and fails again with the identical mismatch, and enough of these in a run trip the `>75% of tasks failing` sev1 orchestration pause. See issue [#2186](https://github.com/sipyourdrink-ltd/bernstein/issues/2186).

**Cause:** The manager's decomposition writes `completion_signals` with exact literal file paths guessed at planning time (e.g. `path_exists: packages/db/test/seed-workers.test.ts`), before any code exists. Workers legitimately place new files at the repo's actual idiomatic location (e.g. `packages/db/src/__tests__/seed-workers-demo-link.test.ts`). The literal path never existed and never will; the check is a coin flip decided before implementation starts, and retries can't fix a decomposition-time guess.

**Diagnosis:**
```bash
grep "janitor failed" .sdd/runtime/*.log
# Look for `path_exists: <path>` entries where the referenced directory
# convention (e.g. `test/` vs `src/__tests__/`) doesn't match what's
# actually in the repo:
find . -name "*<basename-without-extension>*"
```

**Resolution:**
- **Default behavior is unchanged**: `path_exists` with a plain literal
  path still means exactly that path - operators can rely on the check
  meaning the path they wrote.
- **Explicit glob syntax always works**: a criterion that itself contains
  glob characters (e.g. `path_exists: packages/db/**/seed-workers*.test.ts`)
  is evaluated as a glob pattern. This is opt-in per-check by
  construction, and the recommended way for plans/decompositions to
  express "a test file matching this shape exists" without guessing the
  repo's directory convention.
- **Opt-in fuzzy fallback**: set `BERNSTEIN_JANITOR_FUZZY_PATHS=1` to make
  a literal miss fall back to a fuzzy pattern derived from the basename
  (`**/<stem>*<suffix>`), tolerating a different directory
  depth/convention. Default OFF. When a fuzzy match satisfies a check, the
  janitor logs a WARNING (`janitor path_exists: FUZZY MATCH ...`) naming
  the literal path that missed, the pattern tried, and the path that
  matched - check `.sdd/runtime/*.log`. See
  [CONFIG.md](CONFIG.md#3-environment-variables).
- **Known limitation:** `test_passes` signals (e.g. `pnpm exec vitest run
  test/seed-workers.test.ts`) still reference the manager's guessed literal
  path and are not rewritten by this fix - a command-level mismatch there
  will still fail. If you hit this, prefer directory-wide test commands
  (`pnpm --filter db test`) in the plan/decomposition rather than a single
  guessed file path.
- If the manager's decompositions repeatedly guess paths for a repo with an
  unusual layout, consider using `glob_exists` (already supported) instead
  of `path_exists` in hand-authored plan YAML.

---

## Quick Reference: Exit Codes

| Exit code | Meaning | Likely cause |
|-----------|---------|--------------|
| 0 | Success | Normal completion |
| 1 | General error | CLI error, bad config |
| 124 | Timeout | Watchdog killed the agent |
| 126 | Permission denied | Binary not executable |
| 137 | SIGKILL (OOM) | Kernel OOM killer or manual kill -9 |
| -2 | SIGINT | User Ctrl+C |
| -9 | SIGKILL | Force kill |
| -15 | SIGTERM | Graceful shutdown |

## Quick Reference: Log Locations

| File | Purpose |
|------|---------|
| `.sdd/runtime/<session>.log` | Per-agent stdout/stderr |
| `.sdd/runtime/pids/<session>.json` | PID metadata for `bernstein ps` |
| `.sdd/backlog/{open,claimed,closed}/` | YAML task specs (persistent backlog tree) |
| `.sdd/runtime/task-backlog.json` | Shared JSON backlog for external workers (`bernstein backlog claim`) |
| `.sdd/runtime/cost_report.json` | Run cost tracking |
| `.sdd/runtime/access.jsonl` | HTTP request log (rotated at 10 MiB, 1 backup retained) |
| `.sdd/runtime/server.log` | Task server logs |
| `.sdd/runtime/heartbeats/<session>.json` | Live agent progress (updated every 15s) |

## Quick Reference: Diagnostic Commands

```bash
# Check overall system state
bernstein status
bernstein doctor

# Generate a full debug bundle (collects logs, config, metrics, git state)
bernstein debug

# List all tasks by status
curl -s http://127.0.0.1:8052/tasks?status=open | python3 -m json.tool
curl -s http://127.0.0.1:8052/tasks?status=claimed | python3 -m json.tool
curl -s http://127.0.0.1:8052/tasks?status=orphaned | python3 -m json.tool

# Inspect a specific agent session
bernstein ps                                 # list running agents and their PIDs
cat .sdd/runtime/<session>.log               # tail agent output
cat .sdd/runtime/pids/<session>.json         # PID + metadata

# Check cost
bernstein cost                               # spend breakdown
bernstein cost --by model                    # breakdown by model
bernstein cost --last 24h                    # last 24 hours only
cat .sdd/runtime/cost_report.json | python3 -m json.tool

# Run tests via the sharded runner
uv run python scripts/run_tests.py -x

# Prune stale worktrees (safe to run any time)
git worktree prune

# Validate config
python3 -c "import yaml; yaml.safe_load(open('bernstein.yaml'))"
bernstein doctor
```

## Getting Help

If you cannot resolve an issue with this guide:

1. **Run `bernstein doctor`** - the built-in diagnostic prints the most common configuration problems.
2. **Run `bernstein debug`** - generates a debug bundle with logs, config, metrics, and git state for sharing.
3. **Check agent logs** - `.sdd/runtime/<session>.log` contains the full CLI output including provider error messages.
4. **Search GitHub Issues** - many error messages are already tracked at `https://github.com/sipyourdrink-ltd/bernstein/issues`.
5. **File a bug report** - include the output of `bernstein debug`, the relevant log snippet, and your `bernstein.yaml` (redact API keys).
