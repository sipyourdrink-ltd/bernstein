# Sandbox backends

Bernstein isolates every spawned agent in a sandbox so multiple agents
running against the same repository cannot stomp on each other's
files, processes, or secrets. Historically the only sandbox type was
a local git worktree. The choice of sandbox is now pluggable - agents
can run inside worktrees, Docker containers, E2B microVMs, Modal
sandboxes, or any backend a plugin author registers.

This document covers:

- The `SandboxBackend` / `SandboxSession` protocol and the
  `WorkspaceManifest` / `SandboxCapability` value objects
- The nine first-party backends (`worktree`, `docker`, `e2b`, `modal`, `daytona`, `blaxel`, `runloop`, `vercel`, `microvm`)
- The `bernstein.sandbox_backends` entry-point group for third-party
  backends

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

- `name: str` - canonical identifier referenced from `plan.yaml`.
- `capabilities: frozenset[SandboxCapability]` - feature flags.
- `async def create(manifest, options=None) -> SandboxSession` -
  provision a fresh sandbox.
- `async def resume(snapshot_id) -> SandboxSession` - restore a
  snapshot; raises `NotImplementedError` if the backend does not
  declare `SandboxCapability.SNAPSHOT`.
- `async def destroy(session) -> None` - tear down a session.

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
manager bindings) are intentionally deferred to the storage-sinks
work.

## First-party backends

| Backend | Ships in | `capabilities`                                  | Notes |
|---------|----------|--------------------------------------------------|-------|
| `worktree` | core     | `FILE_RW`, `EXEC`, `NETWORK`                     | Wraps the existing `WorktreeManager`. Zero behaviour change. Default. |
| `docker`   | core     | `FILE_RW`, `EXEC`, `NETWORK`                     | Launches a container per session via the `docker` Python SDK. Needs `pip install bernstein[docker]`. |
| `e2b`      | `[e2b]` extra | `FILE_RW`, `EXEC`, `NETWORK`, `SNAPSHOT`     | Runs in E2B Firecracker microVMs. Needs `pip install bernstein[e2b]` plus `E2B_API_KEY`. |
| `modal`    | `[modal]` extra | `FILE_RW`, `EXEC`, `NETWORK`, `SNAPSHOT`, `GPU` | Serverless containers with optional GPU. Needs `pip install bernstein[modal]` plus `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`. |
| `microvm`  | core     | `FILE_RW`, `EXEC`, `NETWORK`, `SNAPSHOT`         | Firecracker microVM per session — isolates kernel / network / PID namespace at a hardware boundary. Snapshots are **content-addressed** (the snapshot id *is* the SHA-256 of the image bytes in CAS). Opt-in: not a free backend, so the heuristic path never auto-selects it; an explicit `sandbox.backend: microvm` on a host without KVM fails loudly rather than degrading isolation. **The VM boot path is experimental (not yet implemented) — see below.** |

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
- **Capabilities.** `e2b`, `modal`, `daytona`, `runloop`, `vercel`, and `microvm` support snapshot/resume (as does the local `worktree`);
  only `modal` exposes GPU today.
- **Supported exec semantics.** All four backends handle argv-based
  exec with exit-code, stdout, and stderr capture.

## MicroVM backend and deterministic fork-and-race

The `microvm` backend (`src/bernstein/core/sandbox/backends/microvm.py`)
adds two things the rest of the sandbox layer was missing: a real
kernel/network/PID boundary, and a snapshot contract strong enough to
build reproducible, auditable races on.

> **Status: the deterministic core (snapshot / fork-race / signed receipt)
> is complete and fully tested; the Firecracker VM boot itself is
> experimental and not yet implemented.** `FirecrackerMonitor` ships the
> host preflight and the strict no-silent-downgrade contract; the full boot
> lifecycle (API socket, drives, networking, `InstanceStart`, and an
> in-guest vsock agent for exec/file-IO) is a tracked follow-up. It cannot
> be built or validated without a KVM-capable Linux host plus an
> operator-supplied kernel, rootfs, and guest agent, so `boot()` raises
> `MicroVMUnavailableError` on every host today rather than pretending. All
> the guarantees below are exercised host-independently over the
> `FakeMonitor`.

**Monitor shim.** The backend never talks to a hypervisor directly. It
drives a `VMMonitor` adapter (`backends/_vmmonitor.py`): a
`FirecrackerMonitor` (production; strict host preflight for KVM +
`firecracker` binary + kernel/rootfs) and a `FakeMonitor` (a
deterministic, host-portable stand-in used by the tests that really
executes commands and really freezes the workspace — not canned bytes).
A Cloud Hypervisor variant fits behind the same shim and is deferred.

**Content-addressed snapshots.** `snapshot()` freezes the workspace into a
*canonicalised image* (a tar with sorted paths, zeroed mtimes/uids,
normalised modes — deterministic given identical file contents), streams
it into the CAS store (`.sdd/cas`, see
[cas-store.md](./cas-store.md)), and returns the **SHA-256 digest** as the
snapshot id. `resume(digest)` reads the blob back with integrity
verification on, so a tampered snapshot fails its CAS check
(`CASIntegrityError`) *before* it can boot. Images are full and
self-contained, so a resume can never be confused about which base it
forked from. Memory snapshots are deliberately out of scope: a memory
image is never byte-reproducible (kernel timers, entropy, page/ASLR
ordering), which would make the determinism guarantee below impossible.

**Fork-and-race** (`src/bernstein/core/sandbox/fork_race.py`).
`fork_race()` resumes K candidates from *one* content-addressed base
digest, runs each to a terminal snapshot, and picks the winner with the
existing deterministic ranker (`select_winner` → TOPSIS) — **no LLM in the
selection path**. Determinism is engineered end to end: candidates are
sorted by `task_id` *before* ranking (float sums are order-sensitive), and
the pinned ranking profile excludes any wall-clock axis.

**Selection receipt** (`src/bernstein/core/sandbox/selection_receipt.py`).
The output is a `SelectionReceipt`: canonical JSON, Ed25519-signed, binding
`{base_snapshot_digest, candidates[{task_id, terminal_snapshot_digest,
score_vector, isolation}], winner_task_id, winner_snapshot_digest,
ranker_profile, loser_snapshot_digests[]}`. The signed body carries **no**
wall-clock, run id, or chain position, so running the same race twice
produces a **byte-identical** signed receipt. Losing branches are recorded
as lineage siblings, and the receipt is appended to the HMAC-chained audit
log in a single serialised call (chain-position binding lives in that
wrapper entry, not in the receipt body).

**CLI.**

```bash
# Fork K candidates from a base snapshot; emits a signed receipt.
bernstein sandbox fork-race --base <sha256> --k 3 --cmd 'make test' --out receipt.json

# Verify a receipt: Ed25519 signature + re-hash base + winner + every loser
# against CAS. Proves signed + CAS-intact; NOT that it was chain-appended
# (that is the audit log's own verify).
bernstein sandbox receipt verify receipt.json

# Anchor the check to a known signer. WITHOUT this the signature is only
# checked for self-consistency (a receipt re-signed under any key still
# passes) - so an unanchored verify never exits 0.
bernstein sandbox receipt verify receipt.json --expected-keyid <keyid>
```

**Exit codes.** `receipt verify` distinguishes every outcome so a CLI-scripted
gate can branch on them; the same verdict is computed once (`verify_receipt_full`
→ `FullReceiptVerdict`) and shared by the CLI and the library, so the two can
never disagree. A malformed or unreadable receipt file yields a clean
diagnosable error, never a traceback.

| Code | Verdict | Meaning |
|---|---|---|
| 0 | `verified` | Anchored to the expected signer, signature + consistency intact, and every named blob (base + winner + losers) re-hashed intact against CAS. |
| 1 | `failed` | Bad signature/consistency, a **tampered** blob (present, wrong hash), or a **malformed** digest field. Highest precedence — an integrity alarm. |
| 4 | `unreadable` | A named blob could not be read on this host (permissions, or an anomalous symlinked blob the verifier refuses to dereference). A property of the reader, not the record. |
| 2 | `incomplete` | A named blob is **absent** from CAS (GC / retention / restart). An ordinary operational event, never conflated with tampering. |
| 3 | `unanchored` | Signature + blobs check out, but `--expected-keyid` was omitted or empty (an unset env var counts as empty), so *whose* key signed it is unproven. |

Precedence when several apply: `failed` > `unreadable` > `incomplete` (absent) > `unanchored` > `verified`.

`fork-race` requires a microVM-capable host; on an unsupported host it
fails loudly. The determinism/tamper guarantees are validated
host-independently over the `FakeMonitor` (see
`tests/unit/sandbox/test_fork_race.py`); the real Firecracker boot is
covered by the KVM-gated `tests/integration/sandbox/test_microvm_firecracker.py`.

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
worktree backend - byte-identical to the pre-pluggable-sandbox
behaviour.

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

## Integration

- `SandboxBackend` / `SandboxSession` / `SandboxCapability` /
  `WorkspaceManifest` live in `src/bernstein/core/sandbox/`.
- First-party backends ship in core (worktree, docker, microvm, blaxel,
  daytona, runloop, vercel); e2b and modal ship as optional extras.
- `AgentSpawner` accepts an optional `sandbox_session` parameter; when
  `None` it falls back to the direct-worktree path.
- `bernstein agents sandbox-backends` lists installed backends.
- `plan.yaml` accepts an optional `sandbox:` block per stage.

## Observability

Each backend create/destroy cycle emits WAL + Prometheus metrics:

- `sandbox_session_created{backend=..., session_id=...}`
- `sandbox_session_destroyed{backend=..., duration_seconds=...}`
- `sandbox_exec_count{backend=..., exit_code=...}`

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
