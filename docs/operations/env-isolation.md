# Environment variable isolation

When Bernstein spawns a CLI coding agent, the agent subprocess only
receives the environment variables it actually needs. Without this
filter, every agent would inherit the full orchestrator environment -
database URLs, CI tokens, every API key the operator has loaded - and
any of those could leak into a tool call, a prompt, an HTTP request,
or worst of all, a commit message.

This page explains the allowlist behaviour, how to extend it, and how
to verify that a given adapter is actually filtering as designed.

---

## Why env isolation matters

The threat model is simple: an LLM-driven coding agent is an arbitrary
code execution surface that runs *under your shell environment*.
Anything in `os.environ` that the agent's subprocess can read is, by
default, available to:

- the agent's own LLM (via the prompt or tool output),
- tools the agent invokes (which may make outbound HTTP calls),
- any process the agent spawns,
- log lines, error messages, and stack traces that may end up on disk
  or in a shared dashboard.

A leaked `DATABASE_URL` is a production incident. A leaked
`AWS_SECRET_ACCESS_KEY` is a billing incident. A leaked CI token is a
supply-chain incident. The cheapest fix is to never put them in front
of the agent in the first place.

Bernstein's answer is `build_filtered_env()`: a pure function that
returns a fresh `dict[str, str]` containing only the keys on a known
base allowlist plus a small set of per-adapter extras. Every spawn
adapter passes this dict explicitly to `subprocess.Popen(env=...)`.

---

## What gets through the filter

```
filtered_env = _BASE_ALLOWLIST ∪ {adapter_specific_keys} ∩ os.environ
```

The set is the intersection with `os.environ` - keys you don't
actually have set don't appear in the result, and missing keys never
raise.

### Base allowlist (always included if present)

These are the variables every coding agent realistically needs to
function in a Unix-like environment:

| Group               | Vars |
|---------------------|------|
| Shell basics        | `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL` |
| Locale              | `LANG`, `LC_ALL`, `LC_CTYPE`, `LC_MESSAGES` |
| Terminal            | `TERM`, `COLORTERM`, `COLUMNS`, `LINES` |
| Temp dirs           | `TMPDIR`, `TMP`, `TEMP` |
| XDG                 | `XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` |
| Git identity        | `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL` |
| SSH / git transport | `SSH_AUTH_SOCK`, `GIT_SSH_COMMAND`, `GIT_SSH` |
| Python              | `PYTHONPATH`, `VIRTUAL_ENV`, `CONDA_DEFAULT_ENV`, `CONDA_PREFIX` |
| Node                | `NVM_DIR`, `NVM_BIN`, `NVM_PATH`, `NODE_PATH` |

There is no built-in proxy entry. If you run behind a corporate proxy
you probably want `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY` -
currently you have to add these as extras.

### Per-adapter extras

Each adapter passes its own allowlist of API-key-style variables to
`build_filtered_env(extra_keys=[...])`:

| Adapter      | Extra keys |
|--------------|------------|
| Claude Code  | `ANTHROPIC_API_KEY` |
| Codex        | `OPENAI_API_KEY`, `OPENAI_ORG_ID`, `OPENAI_BASE_URL` |
| Gemini       | `GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS` |
| Qwen         | `OPENAI_API_KEY`, `OPENAI_BASE_URL` |
| Aider        | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY` |
| Amp          | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SRC_ENDPOINT`, `SRC_ACCESS_TOKEN` |
| Generic      | (base only - no API keys) |
| Manager      | `ANTHROPIC_API_KEY` |

Anything not in the base allowlist or the per-adapter list is
**dropped** before the subprocess starts. Your `DATABASE_URL`,
`AWS_SECRET_ACCESS_KEY`, `STRIPE_SECRET_KEY` etc. never reach the
agent unless you deliberately add them.

### Special case: Claude Code

`ClaudeCodeAdapter` spawns two processes (the `bernstein-worker` and
the stream-json wrapper). Both receive the same filtered env dict.
The wrapper is a small Python subprocess that imports stdlib only and
does not strictly need `ANTHROPIC_API_KEY`, but receives it
harmlessly so a single env build serves both spawns.

### Worker inheritance

The `bernstein-worker` subprocess itself is launched with the
filtered env. When it then spawns the agent CLI, it does so with no
explicit `env=` parameter - the agent CLI inherits the already-filtered
env via OS-level process inheritance. This is the intended design,
not a leak.

---

## Configuration

### YAML

There is **no** YAML or CLI flag to disable the filter at the
orchestrator level - `build_filtered_env()` is called unconditionally
by every adapter. This is deliberate: the cost of an accidental
"filter off" toggle outweighs any operator convenience.

What you *can* do is extend the allowlist by editing
`src/bernstein/adapters/env_isolation.py` (`_BASE_ALLOWLIST`) or by
adding to a specific adapter's `extra_keys` list.

### Per-adapter override

To add a variable for one adapter only, edit that adapter's call site
(typically `src/bernstein/adapters/<name>.py`) and append to the
`extra_keys` list passed to `build_filtered_env`:

```python
filtered = build_filtered_env(
    extra_keys=[
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BEDROCK_BASE_URL",   # new
    ]
)
```

Recommended hygiene before adding a key:

1. Confirm the key is genuinely needed at agent runtime (not just at
   orchestrator startup).
2. Confirm it is *not* a secret you would be unhappy to see in a
   prompt log.
3. If it is a secret, prefer feeding it through the credential vault
   (`bernstein creds`) rather than the env.

---

## Verifying isolation works

The shortest test recipe:

1. Set a fake secret in your shell:

   ```bash
   export DATABASE_URL='postgres://leak-me:xxx@example/db'
   export PROOF_ENV='isolation-canary'
   ```

2. Run an agent with a tiny goal that prints its environment:

   ```bash
   bernstein -g "Run 'env' and write the output to env.txt, then stop."
   ```

3. After the run, inspect `env.txt`. You should see `PATH`, `HOME`,
   the relevant API key, and **none** of `DATABASE_URL` or
   `PROOF_ENV`.

If a forbidden var leaks through, check:

- Did the adapter pass `env=filtered` to its `Popen` call? Spy on it
  with the test pattern below.
- Did the worker accidentally call `os.environ.copy()` again at any
  point in your fork?

### Unit-test pattern

The shipped test suite (in `tests/unit/test_env_isolation.py` and the
adapter-level Popen-spy tests) covers the base allowlist, secret
exclusion, per-adapter extras, and per-adapter Popen pass-through
cases. The skeleton if you need to add an adapter:

```python
def test_my_adapter_filters_env(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kw):
        captured["env"] = kw.get("env")
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setenv("DATABASE_URL", "should-not-leak")
    monkeypatch.setenv("MY_API_KEY", "should-leak")

    adapter = MyAdapter()
    adapter.spawn(prompt="x", workdir=Path("/tmp"), ...)

    assert captured["env"] is not None
    assert "DATABASE_URL" not in captured["env"]
    assert captured["env"]["MY_API_KEY"] == "should-leak"
```

If you want a single command that asserts *all* adapters pass `env=`,
look for the parametrised test that walks the adapter registry - it
fails loudly when a new adapter forgets the filter.

---

## Code pointers

| File                                                       | What it does |
|------------------------------------------------------------|--------------|
| `src/bernstein/adapters/env_isolation.py`                  | `build_filtered_env()`, `_BASE_ALLOWLIST` |
| `src/bernstein/adapters/claude.py` (and siblings)          | Per-adapter `extra_keys` list and Popen call site |
| `src/bernstein/core/spawner.py`                            | Calls `adapter.spawn()` (Step 1 of the workflow) |
| `src/bernstein/core/worker.py`                             | The wrapper that inherits the filtered env (Step 4) |

---

## Related

- [Permission modes](../architecture/permission-modes.md) - gates
  *what* a tool can do; env isolation gates *what it can read*.
- [Sandbox backends](../architecture/sandbox.md) - adds filesystem and
  network isolation on top of the env filter.
