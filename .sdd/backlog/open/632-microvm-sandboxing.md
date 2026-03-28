# 632 — MicroVM Sandboxing

**Role:** devops
**Priority:** 4 (low)
**Scope:** large
**Depends on:** none

## Problem

Agent code execution has no hardware-level isolation. Agents run with the same privileges as the user, which is a security risk for untrusted code. Git worktrees provide logical isolation but not security isolation.

## Design

Integrate E2B or Microsandbox for sandboxed agent code execution using Firecracker microVMs. MicroVMs boot in under 125ms and provide hardware-level isolation with minimal overhead. Implement a tiered execution model: worktrees for trusted agents (fast, no overhead), microVMs for untrusted agents (secure, slight latency). The spawner decides the isolation tier based on the agent's trust level and the task's security requirements. Configure microVM specs: CPU, memory, filesystem mounts, network access. Support both E2B (cloud-hosted) and Microsandbox (self-hosted) backends. The agent adapter passes the microVM connection info instead of local filesystem paths. Provide a fallback to Docker containers for environments where Firecracker is unavailable.

## Files to modify

- `src/bernstein/core/sandbox.py` (new)
- `src/bernstein/core/spawner.py`
- `src/bernstein/adapters/base.py`
- `.sdd/config.toml` (sandbox configuration)
- `docs/sandboxing.md` (new)
- `tests/unit/test_sandbox.py` (new)

## Completion signal

- Untrusted agents execute inside a microVM or container
- MicroVM boots in under 500ms
- Fallback to Docker works when Firecracker is unavailable
