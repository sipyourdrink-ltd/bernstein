# WORKFLOW: Environment Variable Isolation for Spawned Agents
**Version**: 1.0
**Date**: 2026-03-28
**Author**: Workflow Architect
**Status**: Approved
**Implements**: #347b

---

## Overview
When Bernstein spawns a CLI coding agent, the agent subprocess should only
receive the environment variables it needs to function.  Without filtering,
every agent inherits the full orchestrator environment — database credentials,
CI tokens, API keys for other services, and any other secrets the operator has
loaded.  This workflow defines how agent subprocesses get a filtered
environment at spawn time, and what the allowlist contains.

---

## Actors
| Actor | Role |
|---|---|
| Orchestrator | Drives the tick loop; calls adapters to spawn agents |
| Spawner | Assembles the prompt and calls `adapter.spawn()` |
| CLI Adapter | Builds the subprocess command; builds the filtered `env` dict |
| `build_filtered_env()` | Pure function in `env_isolation.py`; constructs the safe env dict |
| `subprocess.Popen` | OS primitive that receives the filtered env and executes the agent |
| Agent CLI process | The external CLI (claude, codex, gemini, etc.) that reads the env |
| Worker wrapper | Internal `bernstein-worker` subprocess; inherits filtered env from its parent |

---

## Prerequisites
- `os.environ` is populated in the orchestrator process (always true).
- The adapter for the target CLI is selected and instantiated.
- `env_isolation.py` is importable by all adapter modules.

---

## Trigger
`adapter.spawn()` is called by the spawner for every new agent session.

---

## Workflow Tree

### STEP 1: Spawner calls adapter.spawn()
**Actor**: Spawner (`src/bernstein/core/spawner.py`)
**Action**: Renders prompt, selects adapter, calls `adapter.spawn(prompt=..., workdir=..., model_config=..., session_id=...)`
**Input**: `{ prompt: str, workdir: Path, model_config: ModelConfig, session_id: str }`
**Output on SUCCESS**: `SpawnResult(pid, log_path)` → GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(adapter not found)`: Adapter registry returns None → orchestrator marks task `failed` with message

**Observable states during this step**:
  - Orchestrator: task transitions from `claimed` → `in_progress`

---

### STEP 2: Adapter calls build_filtered_env()
**Actor**: CLI Adapter (`src/bernstein/adapters/{claude,codex,gemini,qwen,aider,amp,generic,manager}.py`)
**Action**: Calls `build_filtered_env(extra_keys=[<adapter_api_key_name(s)>])`
**Input**: `os.environ` (read-only reference)
**Output on SUCCESS**: `dict[str, str]` containing only allowed vars → GO TO STEP 3

This step cannot fail (pure dict comprehension over os.environ).

**Allowlist composition**:
```
filtered_env = _BASE_ALLOWLIST ∪ {adapter_specific_keys} ∩ os.environ
```

**Base allowlist** (always included if present in env):
```
PATH, HOME, LANG, LC_ALL, LC_CTYPE, LC_MESSAGES,
USER, LOGNAME, SHELL, TERM, COLORTERM, COLUMNS, LINES,
TMPDIR, TMP, TEMP,
XDG_RUNTIME_DIR, XDG_CONFIG_HOME, XDG_DATA_HOME, XDG_CACHE_HOME,
GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, GIT_COMMITTER_NAME, GIT_COMMITTER_EMAIL,
SSH_AUTH_SOCK, GIT_SSH_COMMAND, GIT_SSH,
PYTHONPATH, VIRTUAL_ENV, CONDA_DEFAULT_ENV, CONDA_PREFIX,
NVM_DIR, NVM_BIN, NVM_PATH, NODE_PATH
```

**Per-adapter extra keys**:
| Adapter | Extra keys |
|---|---|
| Claude Code | `ANTHROPIC_API_KEY` |
| Codex | `OPENAI_API_KEY`, `OPENAI_ORG_ID`, `OPENAI_BASE_URL` |
| Gemini | `GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS` |
| Qwen | `OPENAI_API_KEY`, `OPENAI_BASE_URL` |
| Aider | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY` |
| Amp | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SRC_ENDPOINT`, `SRC_ACCESS_TOKEN` |
| Generic | (base only — no adapter-specific keys) |
| Manager | `ANTHROPIC_API_KEY` |

---

### STEP 3: Adapter calls subprocess.Popen with env=filtered_env
**Actor**: CLI Adapter
**Action**: `subprocess.Popen(wrapped_cmd, cwd=workdir, env=filtered_env, ...)`
**Input**: `{ cmd: list[str], env: dict[str, str] }`
**Output on SUCCESS**: `Popen` object with PID → GO TO STEP 4
**Output on FAILURE**:
  - `FAILURE(FileNotFoundError)`: CLI binary not in PATH → raises `RuntimeError("X not found in PATH...")`
  - `FAILURE(PermissionError)`: Binary not executable → raises `RuntimeError("Permission denied...")`

**Observable states during this step**:
  - OS: new process created with restricted environment
  - Agent process: only sees allowed vars

**Special case — ClaudeCodeAdapter**:
Claude spawns TWO processes (the bernstein-worker and the stream-json wrapper).
Both receive the same filtered env dict.  This is important because the wrapper
is a Python subprocess that must import stdlib modules — it does not need
ANTHROPIC_API_KEY but receives it harmlessly.

---

### STEP 4: Worker inherits filtered env and spawns agent CLI
**Actor**: `bernstein-worker` (`src/bernstein/core/worker.py`)
**Action**: Calls `subprocess.Popen(cmd)` — no explicit `env=` parameter.
Because the worker process itself was launched with the filtered env, it
inherits that filtered env into the agent CLI via OS-level process inheritance.

**Observable states during this step**:
  - Agent CLI process: sees only the filtered vars, not the full orchestrator env

---

## State Transitions

```
[spawn requested]
  -> build_filtered_env() (pure, cannot fail)
  -> Popen(env=filtered) called
     -> SUCCESS: agent running with restricted env
     -> FAILURE(binary not found): RuntimeError propagates to orchestrator, task -> failed
     -> FAILURE(permission denied): RuntimeError propagates to orchestrator, task -> failed
```

---

## Handoff Contracts

### Adapter → subprocess.Popen
**Call**: `subprocess.Popen(cmd, cwd=workdir, env=filtered_env, ...)`
**Invariant**: `filtered_env` contains no keys outside `_BASE_ALLOWLIST ∪ extra_keys`
**Invariant**: `filtered_env` is a fresh `dict[str, str]` (not a reference to `os.environ`)

---

## Cleanup Inventory
This workflow creates no persistent resources.  If Popen fails, no cleanup is needed.

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | Worker.py line 81 spawns agent CLI without explicit env= — inherits from worker process | None (by design) | Step 4 | Documented as intended: filtered env propagates via OS inheritance |
| RC-2 | Qwen adapter previously used `os.environ.copy()` which included all secrets | High | Step 2 | Fixed: replaced with `build_filtered_env()` |
| RC-3 | All adapters except Qwen previously passed no env= to Popen (full inheritance) | Critical | Step 3 | Fixed: all adapters now pass explicit `env=` |
| RC-4 | Manager adapter previously did `os.environ.copy()` | High | Step 2 | Fixed: replaced with `build_filtered_env(["ANTHROPIC_API_KEY"])` |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Base vars always included | `build_filtered_env()` called with PATH, HOME, LANG in env | All three present in result |
| TC-02: Secrets excluded | Env contains DATABASE_URL, AWS_SECRET_ACCESS_KEY | Neither present in result |
| TC-03: Extra key included | `build_filtered_env(["ANTHROPIC_API_KEY"])` with key set | API key present in result |
| TC-04: Missing extra key omitted | Key in extra_keys but not in env | Key absent from result (no KeyError) |
| TC-05: Empty environment | os.environ is empty | Returns `{}` |
| TC-06: Result is independent copy | Mutate result dict | Next call unaffected |
| TC-07: Multiple extra keys | Three extra keys provided | All three included |
| TC-08: Git vars forwarded | GIT_AUTHOR_NAME etc. set in env | Present in result |
| TC-09: Codex adapter passes env= | Popen spy | `env=` kwarg present and not None |
| TC-10: Gemini adapter passes env= | Popen spy | `env=` kwarg present and not None |
| TC-11: Claude adapter passes env= to both procs | Popen spy | Both calls have `env=` |
| TC-12: Claude env includes ANTHROPIC_API_KEY | Popen spy with fake env | API key in first call's env |
| TC-13: Claude env excludes unrelated secrets | Popen spy with fake env | SECRET_DB absent |
| TC-14: Aider adapter passes env= | Popen spy | `env=` kwarg present |
| TC-15: Amp adapter passes env= | Popen spy | `env=` kwarg present |
| TC-16: Generic adapter passes env= | Popen spy | `env=` kwarg present |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `bernstein-worker` does not need any secrets from the orchestrator env | Verified: worker.py only uses stdlib, writes PID file, forwards signals | Low — if worker needs a secret, add it to the base allowlist |
| A2 | Claude Code CLI needs only `ANTHROPIC_API_KEY` from the secrets namespace | Verified: Claude Code reads `ANTHROPIC_API_KEY` from env | Medium — Claude Code may read other env vars for proxy settings etc. |
| A3 | The NVM vars in the base allowlist are sufficient for Node.js-based CLIs to work | Not fully verified — depends on installation method | Medium — if `nvm exec` is used, additional vars may be needed |
| A4 | Python `sys.executable` is a full absolute path, so `PATH` is not strictly needed for the worker | Verified: `build_worker_cmd` uses `sys.executable` directly | Low |

## Open Questions
- Should `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` be added to the base allowlist for corporate proxy environments?
- Should a per-role override mechanism be wired from `SeedConfig` through the spawner to adapters?
- Should `build_filtered_env` accept a `mode` flag (e.g. `strict` vs `permissive`) for debugging?

## Spec vs Reality Audit Log
| Date | Finding | Action taken |
|---|---|---|
| 2026-03-28 | Initial spec created; all adapters updated | RC-1 through RC-4 resolved |
