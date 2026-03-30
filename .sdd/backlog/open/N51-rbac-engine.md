# N51 — RBAC Engine

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Bernstein has no access control — any user with CLI access can execute any command, making it unsuitable for teams where operators and viewers need different permission levels.

## Solution
- Define three roles in `.sdd/config/rbac.yaml`: `admin`, `operator`, `viewer`
- Admin: all commands
- Operator: `run`, `retry`, `status`
- Viewer: `status`, `logs`, `cost`
- Load RBAC config at CLI startup and check the current user's role before dispatching every command
- Reject unauthorized commands with a clear error message and exit code 1

## Acceptance
- [ ] `.sdd/config/rbac.yaml` exists with role definitions and user-to-role mappings
- [ ] Every CLI command checks role permissions before execution
- [ ] Admin can execute all commands
- [ ] Operator can only execute `run`, `retry`, `status`
- [ ] Viewer can only execute `status`, `logs`, `cost`
- [ ] Unauthorized command returns clear error message and non-zero exit code
