# 627 — Workspace Isolation Policies

**Role:** security
**Priority:** 3 (medium)
**Scope:** medium
**Depends on:** none

## Problem

Agents have unrestricted filesystem and network access within their workspace. There are no configurable isolation policies. Enterprise multi-tenancy requires preventing agents from accessing unauthorized data or making unexpected network calls.

## Design

Implement per-agent workspace isolation with configurable filesystem and network policies. Define isolation profiles in `.sdd/config.toml` under `[isolation]`: which directories an agent can read/write, which network endpoints it can access, and which system commands it can execute. Implement enforcement at the spawner level — the agent adapter configures the underlying CLI agent with the appropriate restrictions. For filesystem isolation, use the agent's worktree as a chroot-like boundary with explicit allowlists for shared directories. For network isolation, configure proxy settings or firewall rules when available. Provide preset profiles: "trusted" (full access), "standard" (project directory only), "restricted" (specific files only, no network). Log all policy violations to the audit system.

## Files to modify

- `src/bernstein/core/isolation.py` (new)
- `src/bernstein/core/spawner.py`
- `src/bernstein/adapters/base.py`
- `.sdd/config.toml`
- `tests/unit/test_isolation.py` (new)

## Completion signal

- Agents restricted to configured filesystem paths
- Policy violations logged to audit system
- Three preset profiles available: trusted, standard, restricted
