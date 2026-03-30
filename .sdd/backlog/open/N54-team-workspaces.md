# N54 — Team Workspaces

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Teams sharing a single Bernstein installation have no isolation — runs, configs, and artifacts from different projects collide in the same `.sdd/` directory.

## Solution
- Implement `bernstein workspace create <name>` to create an isolated `.sdd/` directory per workspace
- Implement `bernstein workspace switch <name>` to change the active workspace
- Each workspace gets its own runs, traces, configs, and artifacts
- Shared configuration inherits from an org-level `.sdd/config/` that applies to all workspaces
- Store workspace registry in `.sdd/config/workspaces.yaml`

## Acceptance
- [ ] `bernstein workspace create <name>` creates an isolated `.sdd/` directory
- [ ] `bernstein workspace switch <name>` changes the active workspace context
- [ ] Runs and artifacts are isolated per workspace
- [ ] Org-level config values are inherited by all workspaces
- [ ] Workspace-level config can override org-level values
- [ ] `bernstein workspace list` shows all workspaces with active indicator
