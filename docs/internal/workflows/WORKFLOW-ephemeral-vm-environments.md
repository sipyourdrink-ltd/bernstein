# WORKFLOW: Ephemeral Agent Environments Using Lightweight VMs (Firecracker/gVisor)
**Version**: 0.1
**Date**: 2026-04-11
**Author**: Workflow Architect
**Status**: Draft
**Implements**: road-115 — Ephemeral agent environments using lightweight VMs

---

## Overview

For maximum isolation, Bernstein can run agents in lightweight VMs instead of
Docker/Podman containers. Firecracker VMs boot in <125ms and provide
hardware-level isolation — each agent gets a fresh VM with the repo mounted
read-write. This workflow specifies the full lifecycle: VM image preparation,
boot, agent execution, result extraction, and cleanup.

This extends the existing container isolation system (`container.py`,
`sandbox.py`) which already defines `ContainerRuntime.FIRECRACKER` and
`ContainerRuntime.GVISOR` as enum values but has no VM-specific lifecycle
management. The existing `ContainerManager` drives Docker/Podman via CLI — this
workflow adds a parallel `VMManager` path for Firecracker/gVisor runtimes with
different boot, mount, and cleanup semantics.

**Key difference from container isolation**: Containers share the host kernel.
VMs provide a separate kernel per agent, preventing kernel-level attacks
(container escapes, `/proc` leaks, ptrace exploits). This is the right choice
when agents run untrusted code or when compliance requires hardware-level
isolation boundaries.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Orchestrator (`orchestrator.py`) | Decides isolation mode per task based on config |
| Spawner (`spawner.py`) | Routes to VM spawn path when `IsolationMode.VM` is selected |
| VM Manager (new: `vm_manager.py`) | Manages Firecracker/gVisor VM lifecycle: create, exec, extract, destroy |
| Image Builder (new: `vm_images.py`) | Builds and caches rootfs images for agent VMs |
| Worktree Manager (`worktree.py`) | Creates git worktree before VM boot (repo data source) |
| Agent Adapter (`adapters/`) | Executes inside the VM — unchanged from container path |
| Heartbeat Monitor (`heartbeat.py`) | Monitors agent liveness via file-based heartbeat (shared mount) |
| Janitor (`janitor.py`) | Verifies completion and triggers cleanup after VM exit |
| Network Policy (`network_isolation.py`) | Configures VM network (tap device or none) |

---

## Prerequisites

- **Firecracker path**: `firecracker` binary installed, `/dev/kvm` available (requires bare-metal or nested virt), `jailer` binary for production use
- **gVisor path**: `runsc` binary installed, OCI-compatible container runtime configured
- Host kernel supports KVM (`/dev/kvm` exists and is accessible)
- Root filesystem image exists at configured path (or auto-build is enabled)
- Sufficient disk space for VM rootfs snapshots (typically 500MB-2GB per image)
- Agent adapter binary/runtime available inside the rootfs image

---

## Trigger

VM-based isolation is triggered when:
1. `OrchestratorConfig.isolation_mode = "vm"` (global), OR
2. A task has `isolation: vm` in its definition (per-task override), OR
3. A role template specifies `isolation: vm` (per-role override)

The spawner checks isolation mode in `spawn_for_tasks()` and routes to
`VMManager` instead of `ContainerManager` or subprocess spawn.

**Decision point in `spawner.py`**:
```
isolation_mode == NONE      -> subprocess in repo root
isolation_mode == WORKTREE  -> subprocess in worktree (current default)
isolation_mode == CONTAINER -> ContainerManager (Docker/Podman)
isolation_mode == VM        -> VMManager (Firecracker/gVisor) [THIS WORKFLOW]
```

---

## Workflow Tree

### STEP 1: Prepare Worktree
**Actor**: Worktree Manager (`worktree.py`)
**Action**: Create an isolated git worktree for the agent, same as the existing
worktree isolation path. The worktree becomes the workspace mounted into the VM.
  1. Create worktree at `.sdd/worktrees/{session_id}/` on branch `agent/{session_id}`
  2. Run worktree isolation validation (AGENT-002 checks)
  3. Apply `WorktreeSetupConfig` (symlinks, copies, sparse checkout)
**Timeout**: 30s
**Input**: `{ session_id: str, repo_root: Path, setup_config: WorktreeSetupConfig }`
**Output on SUCCESS**: `worktree_path: Path` -> GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(git_error)`: Worktree creation failed (disk full, branch conflict) -> [recovery: fail task with descriptive error, no cleanup needed]
  - `FAILURE(isolation_violation)`: AGENT-002 validation failed -> [recovery: remove worktree, fail task with violation details]

**Observable states during this step**:
  - Customer sees: nothing
  - Operator sees: agent session in `SPAWNING` state
  - Database: `agent.status = "spawning"`, `agent.isolation_mode = "vm"`
  - Logs: `[spawner] creating worktree for VM agent {session_id}`

---

### STEP 2: Resolve or Build VM Image
**Actor**: Image Builder (`vm_images.py`)
**Action**: Ensure a rootfs image is available for the agent's runtime.
  1. Check image cache at `.sdd/vm-images/{image_name}.ext4` (or configured path)
  2. If cached and not stale (mtime < `image_ttl_hours`), use cached image
  3. If missing or stale, build image:
     a. For Firecracker: build ext4 rootfs from base image + agent toolchain
     b. For gVisor: pull OCI image via `docker pull` / `podman pull`
  4. Verify image integrity (checksum comparison if available)

**Image contents** (Firecracker rootfs):
  - Base: minimal Linux (Alpine or Debian slim)
  - Python 3.12+ runtime
  - Git, curl, standard build tools
  - Agent CLI binary (claude, codex, etc.) — adapter-specific
  - `/workspace` mount point for repo worktree
  - `/sdd` mount point for `.sdd/runtime/` communication (heartbeats, signals)

**Timeout**: 300s (image build can be slow; cached path is <1s)
**Input**: `{ adapter_name: str, vm_config: VMConfig }`
**Output on SUCCESS**: `image_path: Path` -> GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(build_failed)`: Image build failed (missing base image, disk full) -> [recovery: fail task, log build output for debugging]
  - `FAILURE(pull_failed)`: OCI pull failed (network error, auth failure) -> [recovery: retry 1x with 10s backoff -> fail task]
  - `FAILURE(integrity_mismatch)`: Cached image corrupted -> [recovery: delete cached image, rebuild from scratch, retry 1x]

**Observable states during this step**:
  - Customer sees: nothing
  - Operator sees: `agent.phase = "image_build"` (if building; skipped if cached)
  - Database: no change
  - Logs: `[vm_images] image cache {hit|miss} for {adapter_name}, path={image_path}`

---

### STEP 3: Configure VM Resources and Network
**Actor**: VM Manager (`vm_manager.py`)
**Action**: Build the VM configuration from `VMConfig` and `NetworkPolicy`.
  1. Allocate resources from `VMConfig`:
     - vCPUs (default: 2)
     - Memory (default: 4096 MB)
     - Disk (rootfs size + workspace overlay)
  2. Configure network:
     - `NONE`: no network device attached (strongest isolation)
     - `TAP`: create tap device with iptables rules for restricted access
     - `HOST_BRIDGE`: bridge to host network (for task server access at localhost:8052)
  3. Configure mounts:
     - Worktree -> `/workspace` (read-write, virtio-blk or virtiofs)
     - `.sdd/runtime/heartbeats/` -> `/sdd/heartbeats` (read-write, for agent heartbeat)
     - `.sdd/runtime/signals/{session_id}/` -> `/sdd/signals` (read-write, for SHUTDOWN/WAKEUP)
  4. Generate Firecracker VM config JSON or gVisor OCI spec
  5. Configure jailer (Firecracker production mode):
     - Chroot jail at `/srv/jailer/firecracker/{session_id}/`
     - UID/GID mapping for non-root execution
     - cgroup limits matching resource allocation

**Timeout**: 5s (config generation only)
**Input**: `{ vm_config: VMConfig, network_policy: NetworkPolicy, worktree_path: Path, session_id: str }`
**Output on SUCCESS**: `VMSpec` (config object) -> GO TO STEP 4
**Output on FAILURE**:
  - `FAILURE(kvm_unavailable)`: `/dev/kvm` not accessible -> [recovery: fall back to `IsolationMode.CONTAINER` if `vm_fallback_to_container = true` in config; otherwise fail task with clear error]
  - `FAILURE(tap_creation_failed)`: Network tap device creation failed (insufficient privileges) -> [recovery: if network mode is optional, proceed with NONE; otherwise fail task]
  - `FAILURE(resource_exhaustion)`: Host cannot allocate requested vCPUs/memory -> [recovery: log current host resource usage, fail task with "insufficient host resources"]

**Observable states during this step**:
  - Customer sees: nothing
  - Operator sees: `agent.phase = "vm_config"`
  - Database: no change
  - Logs: `[vm_manager] configured VM: vcpus={n}, mem={mb}MB, net={mode}, mounts={count}`

---

### STEP 4: Boot VM and Execute Agent
**Actor**: VM Manager (`vm_manager.py`)
**Action**: Start the VM and execute the agent command inside it.

**4a: Boot VM**
  1. For Firecracker:
     - Start `firecracker` (or `jailer` in production) with the generated config
     - VM boots kernel + rootfs in <125ms (Firecracker SLA)
     - Wait for boot completion signal (init writes to virtio-serial or mount point)
  2. For gVisor:
     - Start `runsc run` with the OCI spec
     - Sandbox initializes in <500ms
  3. Verify VM is responsive:
     - Check that the workspace mount is accessible from inside
     - Check that the heartbeat mount is writable

**4b: Execute agent command**
  1. For Firecracker: exec command via virtio-serial or SSH (if tap network is up)
  2. For gVisor: `runsc exec` into the running sandbox
  3. Command format: same as container path — adapter `spawn_command()` output
  4. Environment variables injected via VM config (filtered via `env_isolation.py`)
  5. Agent process starts, writes heartbeat, begins task execution

**Timeout**: `task_timeout_s` (default: 900s per the existing stale claim timeout)
**Input**: `{ vm_spec: VMSpec, cmd: list[str], env: dict[str, str] }`
**Output on SUCCESS**: Agent exits with code 0 -> GO TO STEP 5
**Output on FAILURE**:
  - `FAILURE(boot_timeout)`: VM did not boot within 10s -> [recovery: destroy VM, retry 1x -> ABORT_CLEANUP]
  - `FAILURE(boot_crash)`: Firecracker/runsc exited unexpectedly during boot -> [recovery: log stderr, check kernel/rootfs compatibility, ABORT_CLEANUP]
  - `FAILURE(agent_timeout)`: Agent exceeded `task_timeout_s` -> [recovery: send SHUTDOWN signal via shared mount, wait 30s for graceful exit, then force-kill VM -> GO TO STEP 5 with partial results]
  - `FAILURE(agent_crash)`: Agent exited with non-zero code -> [recovery: GO TO STEP 5 with failure status — results may still be extractable]
  - `FAILURE(vm_oom)`: VM hit memory limit, kernel OOM-killed agent -> [recovery: log OOM, ABORT_CLEANUP with "increase memory_mb" recommendation]
  - `FAILURE(heartbeat_stale)`: No heartbeat update for > stale_threshold -> [recovery: check if VM process is still alive; if dead, GO TO STEP 5; if alive but unresponsive, force-kill VM -> ABORT_CLEANUP]

**Observable states during this step**:
  - Customer sees: nothing (agent is working)
  - Operator sees: `agent.status = "running"`, `agent.isolation_mode = "vm"`, heartbeat updates
  - Database: `agent.status = "running"`, heartbeat timestamp updating
  - Logs: `[vm_manager] VM {session_id} booted in {ms}ms, agent executing`

---

### STEP 5: Extract Results from VM
**Actor**: VM Manager (`vm_manager.py`)
**Action**: After agent exits (success or failure), extract work products from the VM.
  1. Verify worktree mount has changes: `git status` in worktree path
  2. Read agent's completion signal from `.sdd/runtime/heartbeats/{session_id}.json`
  3. Collect agent stderr/stdout logs from VM console output
  4. If Firecracker: results are already on the host filesystem (virtio mount is synchronous)
  5. If gVisor: results are in the OCI rootfs overlay — copy changed files out
  6. Validate extracted files (no symlink escapes, no files outside worktree)

**Timeout**: 30s
**Input**: `{ session_id: str, worktree_path: Path, exit_code: int }`
**Output on SUCCESS**: `{ files_changed: list[str], exit_code: int, logs: str }` -> GO TO STEP 6
**Output on FAILURE**:
  - `FAILURE(mount_inaccessible)`: Worktree mount corrupted or VM filesystem error -> [recovery: log error, mark task as failed with "VM filesystem error", GO TO STEP 6 with empty results]
  - `FAILURE(extraction_timeout)`: File copy from gVisor overlay takes too long -> [recovery: force-kill extraction, use partial results]

**Observable states during this step**:
  - Customer sees: nothing
  - Operator sees: `agent.phase = "extracting"`
  - Database: no change yet
  - Logs: `[vm_manager] extracting results: {n} files changed, exit_code={code}`

---

### STEP 6: Destroy VM and Clean Up
**Actor**: VM Manager (`vm_manager.py`)
**Action**: Tear down the VM and release all host resources.
  1. Stop the VM process:
     - Firecracker: send `PUT /actions` with `InstanceStop` via API socket, then kill process
     - gVisor: `runsc kill` + `runsc delete`
  2. Remove jailer chroot (Firecracker): `rm -rf /srv/jailer/firecracker/{session_id}/`
  3. Remove tap device (if created): `ip link delete tap-{session_id}`
  4. Release cgroup allocations
  5. Remove VM socket file
  6. Worktree cleanup handled by existing janitor flow (unchanged)

**Timeout**: 15s
**Input**: `{ vm_handle: VMHandle }`
**Output on SUCCESS**: All resources released -> DONE (task continues through janitor/approval)
**Output on FAILURE**:
  - `FAILURE(vm_stuck)`: VM process does not respond to stop signal -> [recovery: SIGKILL the process, force-remove resources, log orphan warning]
  - `FAILURE(cleanup_partial)`: Some resources not cleaned up (tap device, cgroup) -> [recovery: log orphaned resources for manual cleanup, continue — do not block task completion]

**Observable states during this step**:
  - Customer sees: nothing
  - Operator sees: `agent.status = "completed"` or `"failed"`
  - Database: agent session marked complete
  - Logs: `[vm_manager] VM {session_id} destroyed, resources released`

---

### ABORT_CLEANUP: VM Failure Cleanup
**Triggered by**: Boot timeout, boot crash, VM OOM, unrecoverable errors
**Actions** (in order):
  1. Force-kill VM process if still running (`kill -9 {pid}`)
  2. Remove jailer chroot directory
  3. Remove tap device if created
  4. Release cgroup allocations
  5. Remove VM socket file
  6. Clean up worktree via `WorktreeManager.cleanup(session_id)`
  7. Mark agent session as `FAILED` with error details
  8. Mark task as `FAILED` with `abort_reason = "vm_failure"`
  9. Log complete failure context for post-mortem

**What customer sees**: Task marked as failed in status
**What operator sees**: Agent in failed state with VM-specific error message, orphan resource warnings if cleanup was partial

---

## State Transitions

```
[task_open] -> (spawner selects VM isolation) -> [vm_spawning]
[vm_spawning] -> (worktree created, image ready) -> [vm_booting]
[vm_booting] -> (VM boots successfully) -> [vm_running]
[vm_running] -> (agent exits 0) -> [vm_extracting] -> [vm_cleanup] -> [task_done]
[vm_running] -> (agent exits != 0) -> [vm_extracting] -> [vm_cleanup] -> [task_failed]
[vm_running] -> (timeout) -> [vm_draining] -> [vm_extracting] -> [vm_cleanup] -> [task_failed]
[vm_booting] -> (boot failure) -> [abort_cleanup] -> [task_failed]
[vm_running] -> (VM OOM) -> [abort_cleanup] -> [task_failed]
[vm_spawning] -> (kvm_unavailable, fallback enabled) -> [container_spawning] (fallback to Docker)
```

---

## Handoff Contracts

### Spawner -> VM Manager (VM creation)
**Method**: `VMManager.create(session_id, vm_config, worktree_path, cmd, env)`
**Payload**:
```python
{
  "session_id": "str — unique agent session ID",
  "vm_config": "VMConfig — resources, network, image",
  "worktree_path": "Path — host path to git worktree",
  "cmd": "list[str] — agent command to execute",
  "env": "dict[str, str] — filtered environment variables"
}
```
**Success response**:
```python
{
  "vm_handle": "VMHandle — opaque handle for lifecycle management",
  "pid": "int — host PID of VM process",
  "boot_time_ms": "int — time from start to ready"
}
```
**Failure response**:
```python
{
  "ok": False,
  "error": "str — human-readable error",
  "code": "str — BOOT_TIMEOUT | BOOT_CRASH | KVM_UNAVAILABLE | OOM | RESOURCE_EXHAUSTION",
  "retryable": "bool — true for transient failures"
}
```
**Timeout**: 10s for boot, then `task_timeout_s` for execution

### VM Manager -> Heartbeat Monitor (liveness)
**Payload**: File-based heartbeat via shared mount
```json
{
  "timestamp": 1712836200,
  "phase": "implementing",
  "progress_pct": 45,
  "current_file": "src/foo.py",
  "message": "working"
}
```
**Contract**: Heartbeat file at `/sdd/heartbeats/{session_id}.json` inside VM,
mapped to `.sdd/runtime/heartbeats/{session_id}.json` on host. Same format as
existing subprocess heartbeats — no VM-specific changes needed.

### VM Manager -> Agent Signal Protocol
**Contract**: Signal files at `/sdd/signals/` inside VM, mapped to
`.sdd/runtime/signals/{session_id}/` on host. Same protocol as existing:
- `SHUTDOWN`: orchestrator -> agent (graceful stop)
- `WAKEUP`: orchestrator -> agent (address concern)
- Shared mount ensures real-time visibility in both directions.

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Git worktree | Step 1 | Step 6 / ABORT_CLEANUP | `WorktreeManager.cleanup()` |
| VM process (firecracker/runsc) | Step 4 | Step 6 / ABORT_CLEANUP | API socket stop + SIGKILL |
| Jailer chroot directory | Step 4 (Firecracker) | Step 6 / ABORT_CLEANUP | `rm -rf /srv/jailer/firecracker/{session_id}/` |
| Tap network device | Step 3 (if TAP mode) | Step 6 / ABORT_CLEANUP | `ip link delete tap-{session_id}` |
| Cgroup allocations | Step 4 | Step 6 / ABORT_CLEANUP | Cgroup directory removal |
| VM API socket file | Step 4 (Firecracker) | Step 6 / ABORT_CLEANUP | File deletion |
| Cached rootfs image | Step 2 | NOT destroyed per-run | TTL-based eviction by image cache |
| Agent branch | Step 1 | Janitor (existing flow) | `git branch -D agent/{session_id}` |

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | `ContainerRuntime.FIRECRACKER` exists in `container.py` but `ContainerManager` only implements Docker/Podman CLI args — no Firecracker API socket protocol | Critical | Step 4 | New `VMManager` class needed; `ContainerManager` unchanged |
| RC-2 | `ContainerRuntime.GVISOR` exists but is treated as a Docker `--runtime=runsc` flag, not a standalone sandbox | Medium | Step 4 | gVisor path uses `runsc` directly for stronger isolation than Docker+runsc |
| RC-3 | `IsolationMode` enum in `models.py` has `NONE`, `WORKTREE`, `CONTAINER` — no `VM` value | High | Trigger | Must add `VM = "vm"` to `IsolationMode` enum |
| RC-4 | `sandbox.py:spawn_in_sandbox` only handles Docker/Podman — no VM path | High | Step 4 | Spawner must route to `VMManager` before reaching `spawn_in_sandbox` |
| RC-5 | Heartbeat and signal paths use host filesystem — VM mount must map these exactly | Medium | Step 3 | Mount configuration must preserve path structure |
| RC-6 | `jailer` binary is required for production Firecracker but not for development — spec should support both modes | Low | Step 3 | Add `use_jailer: bool` config flag (default True in prod, False in dev) |
| RC-7 | No existing VM image build pipeline — must be created from scratch | High | Step 2 | New `vm_images.py` module needed |
| RC-8 | `/dev/kvm` availability varies across environments (CI, laptops, cloud instances) — fallback path essential | Medium | Step 3 FAILURE | Fallback to container isolation when KVM unavailable |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — Firecracker | Valid config, KVM available, image cached | VM boots <125ms, agent executes, results extracted, VM destroyed |
| TC-02: Happy path — gVisor | Valid config, runsc installed | gVisor sandbox starts, agent executes, results extracted, sandbox deleted |
| TC-03: KVM unavailable, fallback enabled | `/dev/kvm` missing, `vm_fallback_to_container = true` | Falls back to Docker container isolation, logs warning |
| TC-04: KVM unavailable, fallback disabled | `/dev/kvm` missing, `vm_fallback_to_container = false` | Task fails with clear "KVM required" error |
| TC-05: Image cache miss | No cached rootfs image | Image built from scratch, cached, then VM boots |
| TC-06: Image cache hit | Cached rootfs exists, not stale | Image reused, no build step |
| TC-07: Boot timeout | Firecracker hangs during boot | VM killed after 10s, retry 1x, then ABORT_CLEANUP |
| TC-08: Agent timeout | Agent runs past `task_timeout_s` | SHUTDOWN signal sent, 30s grace, force-kill, partial results extracted |
| TC-09: VM OOM | Agent exceeds memory_mb limit | Kernel OOM kills agent, ABORT_CLEANUP, error logged |
| TC-10: Heartbeat stale | Agent stops writing heartbeat | VM checked for liveness, killed if unresponsive |
| TC-11: Worktree isolation violation | Shared mount leaks host paths | AGENT-002 validation catches it, VM never boots |
| TC-12: Cleanup after crash | Orchestrator crashes mid-execution | On restart, `cleanup_stale_vms()` finds orphaned VMs and destroys them |
| TC-13: Network isolation — none | `network_mode = "none"` | VM has no network device, agent cannot reach external services |
| TC-14: Network isolation — tap | `network_mode = "tap"` with restricted policy | Agent can reach localhost:8052 only, external blocked |
| TC-15: Concurrent VMs | 5 agents spawned with VM isolation simultaneously | Each gets own VM, no resource conflicts, all complete |
| TC-16: Graceful shutdown signal | Orchestrator sends SHUTDOWN during execution | Agent reads signal from shared mount, commits WIP, exits |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Firecracker boots in <125ms on hosts with KVM | Firecracker documentation; not verified on target infra | Boot timeout set to 10s provides large safety margin |
| A2 | virtio mounts provide real-time file visibility (heartbeats, signals) | Firecracker docs confirm virtio-blk is synchronous | If async, heartbeat detection may lag — increase poll interval |
| A3 | Host has sufficient resources to run multiple VMs concurrently | Not verified — depends on deployment environment | Resource exhaustion error in Step 3 handles this |
| A4 | `jailer` binary is available alongside `firecracker` in production | Not verified | Dev mode works without jailer; prod mode requires it |
| A5 | gVisor `runsc` provides stronger isolation than Docker+runsc | gVisor documentation | If equivalent, no benefit to standalone runsc path — simplify to Docker+runsc flag |
| A6 | Cached rootfs images are not modified by concurrent VMs | Design: each VM gets a copy-on-write overlay | If overlay fails, VMs corrupt shared image — use snapshot per boot |
| A7 | Agent adapters work identically inside VMs as in containers | Not verified for all adapters | Some adapters may require host-specific paths; test each adapter |
| A8 | File-based heartbeat protocol works across VM mount boundaries | Verified for worktrees and containers; not verified for VMs | Mount configuration in Step 3 must be tested end-to-end |

## Open Questions

- Should Firecracker and gVisor share the same `VMManager` interface, or should gVisor
  use the existing `ContainerManager` with `--runtime=runsc`? (Depends on whether
  standalone `runsc` provides meaningfully stronger isolation than Docker+runsc.)
- What base image should the Firecracker rootfs use? Alpine (smaller, faster boot) vs
  Debian slim (more compatible with agent toolchains)?
- Should VM image building be a separate CLI command (`bernstein vm build-image`) or
  automatic on first use? (Separate command is more predictable for operators.)
- How should VM resource limits interact with the existing `resource_limits.py` OS-level
  limits? (VM limits are enforced by hypervisor; OS limits inside VM are redundant but
  provide defense-in-depth.)
- Should there be a warm pool of pre-booted VMs analogous to the existing `WarmPool` for
  containers? (Reduces per-spawn latency from ~125ms to ~0ms but increases host memory usage.)

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-11 | Initial spec created from codebase discovery | — |
| 2026-04-11 | `ContainerRuntime.FIRECRACKER` exists in container.py but has no implementation | Spec defines new VMManager parallel to ContainerManager |
| 2026-04-11 | `IsolationMode` enum lacks VM value | Spec documents required enum addition (RC-3) |
| 2026-04-11 | Existing two-phase sandbox pattern (setup with network, exec without) applicable to VMs | Spec incorporates same pattern for VM network modes |
| 2026-04-11 | `sandbox.py` `DockerSandbox` is Docker/Podman only — VM needs separate config type | Spec defines VMConfig as new dataclass |
