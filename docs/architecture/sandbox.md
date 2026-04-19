# Sandbox backends

Bernstein isolates every spawned agent in a sandbox so multiple agents
running against the same repository cannot stomp on each other's
files, processes, or secrets. Historically the only sandbox type was
a local git worktree. As of oai-002 the choice of sandbox is pluggable
— agents can run inside worktrees, Docker containers, E2B microVMs,
Modal sandboxes, or any backend a plugin author registers.

This document covers:

- The `SandboxBackend` / `SandboxSession` protocol and the
  `WorkspaceManifest` / `SandboxCapability` value objects
- The four first-party backends (`worktree`, `docker`, `e2b`, `modal`)
- The `bernstein.sandbox_backends` entry-point group for third-party
  backends
- The phased rollout plan (phase 1 lands the protocol and backends;
  phase 2 — tracked as `oai-002b` — refactors the spawner to route
  adapter exec through `SandboxSession`)

## Protocol shape

The protocol lives in `src/bernstein/core/sandbox/`:

```python
from bernstein.core.sandbox import (
    SandboxBackend,
    SandboxSession,
    SandboxCapability,
    WorkspaceManifest,
    GitRepoEntry,
    FileEntry,
    ExecResult,
    get_backend,
    list_backends,
    register_backend,
)
```

### `SandboxBackend`

A `runtime_checkable` `Protocol`. Every backend exposes:

- `name: str` — canonical identifier referenced from `plan.yaml`.
- `capabilities: frozenset[SandboxCapability]` — feature flags.
- `async def create(manifest, options=None) -> SandboxSession` —
  provision a fresh sandbox.
- `async def resume(snapshot_id) -> SandboxSession` — restore a
  snapshot; raises `NotImplementedError` if the backend does not
  declare `SandboxCapability.SNAPSHOT`.
- `async def destroy(session) -> None` — tear down a session.

### `SandboxSession`

An `ABC` with six abstract methods:

- `read(path) -> bytes`
- `write(path, data, *, mode=0o644) -> None`
- `exec(cmd, *, cwd=None, env=None, timeout=None, stdin=None) -> ExecResult`
- `ls(path) -> list[str]`
- `snapshot() -> str` (SNAPSHOT-capable backends only)
- `shutdown() -> None` (idempotent)

`ExecResult` is a frozen dataclass with `exit_code`, `stdout`,
`stderr`, and `duration_seconds`.

### `SandboxCapability`

An `StrEnum` with six values: `FILE_RW`, `EXEC`, `NETWORK`, `GPU`,
`SNAPSHOT`, `PERSISTENT_VOLUMES`. Every backend advertises the set
it supports; schedulers reject manifests requiring capabilities the
selected backend does not expose.

### `WorkspaceManifest`

Immutable value object passed to `SandboxBackend.create`:

```python
@dataclass(frozen=True)
class WorkspaceManifest:
    root: str = "/workspace"
    repo: GitRepoEntry | None = None
    files: tuple[FileEntry, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = 1800
```

`GitRepoEntry` and `FileEntry` are companion frozen dataclasses.
Cloud-specific mount entries (S3, persistent volumes, secrets
manager bindings) are intentionally deferred to `oai-003`.

## First-party backends

| Backend | Ships in | `capabilities`                                  | Notes |
|---------|----------|--------------------------------------------------|-------|
| `worktree` | core     | `FILE_RW`, `EXEC`, `NETWORK`                     | Wraps the existing `WorktreeManager`. Zero behaviour change. Default. |
| `docker`   | core     | `FILE_RW`, `EXEC`, `NETWORK`                     | Launches a container per session via the `docker` Python SDK. Needs `pip install bernstein[docker]`. |
| `e2b`      | `[e2b]` extra | `FILE_RW`, `EXEC`, `NETWORK`, `SNAPSHOT`     | Runs in E2B Firecracker microVMs. Needs `pip install bernstein[e2b]` plus `E2B_API_KEY`. |
| `modal`    | `[modal]` extra | `FILE_RW`, `EXEC`, `NETWORK`, `SNAPSHOT`, `GPU` | Serverless containers with optional GPU. Needs `pip install bernstein[modal]` plus `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`. |

### Trade-offs

- **Latency.** `worktree` has no provisioning cost; `docker` adds a
  one-time pull plus ≤ 2 s container start; `e2b` / `modal` add 1–3 s
  of cold start per session plus provider-side overhead.
- **Cost.** `worktree` and `docker` are free (local compute). `e2b`
  bills by sandbox minute. `modal` bills by compute seconds, with
  optional GPU surcharges.
- **Isolation.** `worktree` shares the host filesystem and network;
  `docker` provides cgroup + namespace isolation but shares the
  kernel; `e2b` runs in a fresh Firecracker microVM per session;
  `modal` runs in dedicated serverless containers.
- **Capabilities.** Only `e2b` and `modal` support snapshot/resume;
  only `modal` exposes GPU today.
- **Supported exec semantics.** All four backends handle argv-based
  exec with exit-code, stdout, and stderr capture. `docker` does not
  support stdin injection in phase 1; that is tracked in `oai-002b`.

## `plan.yaml` extension

```yaml
stages:
  - name: risky-execution
    sandbox:
      backend: docker          # worktree (default), docker, e2b, modal, or a plugin name
      options:
        image: python:3.13-slim
        memory_mb: 2048
        timeout_seconds: 1800
    steps:
      - title: "Run untrusted code analysis"
        role: security
        cli: claude
```

`sandbox:` is entirely optional. When omitted the stage runs in the
worktree backend — byte-identical to pre-oai-002 behaviour.

## Registering a custom backend

Plugin authors declare an entry point in their own `pyproject.toml`:

```toml
[project.entry-points."bernstein.sandbox_backends"]
mybackend = "my_package.sandbox:MySandboxBackend"
```

On next process start the registry picks the entry up automatically.
`bernstein agents sandbox-backends` lists every installed backend
with its capability set so operators can verify registration.

Third-party backends must:

1. Provide `name` and `capabilities` class attributes.
2. Implement `create`, `resume`, and `destroy` as coroutines.
3. Pass the conformance suite at
   `bernstein.core.sandbox.conformance.SandboxBackendConformance`.
4. Import provider SDKs lazily (inside methods or behind
   `TYPE_CHECKING`) so importing the backend module never crashes on
   a missing SDK.

## Phased rollout

### Phase 1 (this ticket, `oai-002`)

- `SandboxBackend` / `SandboxSession` / `SandboxCapability` /
  `WorkspaceManifest` land in `src/bernstein/core/sandbox/`.
- Four first-party backends ship (worktree & docker in core; e2b &
  modal as optional extras).
- `AgentSpawner` gains an optional `sandbox_session` parameter. When
  `None` it falls back to the existing direct-worktree path. All 35
  adapters continue to run unchanged.
- `bernstein agents sandbox-backends` lists installed backends.
- `plan.yaml` accepts an optional `sandbox:` block per stage.

### Phase 2 (follow-up, `oai-002b`)

- `AgentSpawner` routes adapter exec through
  `SandboxSession.exec`, so the selected backend controls where
  subprocesses actually run.
- Adapters are refactored one-by-one to use `session.read` /
  `session.write` for file I/O instead of direct `Path.write_bytes`
  calls on the worktree directory.
- Cost metrics and WAL events gain backend-aware labels.

Phase 2 is a mechanical but widespread refactor across 35 adapters;
keeping it out of phase 1 lets the protocol land independently.

## Observability (phase 1 scaffolding)

Each backend create/destroy cycle should emit WAL + Prometheus
metrics:

- `sandbox_session_created{backend=..., session_id=...}`
- `sandbox_session_destroyed{backend=..., duration_seconds=...}`
- `sandbox_exec_count{backend=..., exit_code=...}`

Wiring into the existing metrics/WAL subsystems is part of
`oai-002b`; phase 1 only exposes the interfaces.

## Conformance

`SandboxBackendConformance` (in
`src/bernstein/core/sandbox/conformance.py`) is a parametrised pytest
class any backend can subclass to get a complete protocol test
coverage suite. Backends declaring `SANDBOX_CAPABILITY.SNAPSHOT`
additionally get the snapshot/resume round-trip test automatically.

The worktree backend runs the conformance suite in unit tests
(`tests/unit/sandbox/test_backend_protocol.py`). Docker / E2B /
Modal conformance lives under `tests/integration/sandbox/`; those
tests auto-skip without a live daemon or provider credentials.

## Security considerations

- `worktree` does **not** isolate at the kernel level. If you need
  to run untrusted code you must choose a sandboxed backend.
- `docker` should be run with `network_disabled=True` for untrusted
  workloads; the default leaves network enabled because most agent
  tasks legitimately need outbound HTTP.
- `e2b` and `modal` run untrusted code by design; their isolation
  posture is the provider's responsibility.
- Snapshot IDs are opaque to callers but may contain sensitive
  state. Do not log them at INFO level without redaction.
