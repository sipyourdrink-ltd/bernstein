# N72 — Admin Console

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Administrators must SSH into servers and edit YAML files to manage users, policies, and system health — there is no graphical admin interface for routine management tasks.

## Solution
- Add an admin section to the web dashboard (N71) behind admin role check (N51)
- User management page: list users, add new users, remove users, assign roles
- Policy editor page: edit YAML policies with syntax validation and preview
- System health page: provider status (up/down/degraded), disk usage, memory usage
- All changes via admin console are audit-logged

## Acceptance
- [ ] Admin section is accessible only to users with the admin role
- [ ] User management page supports list, add, remove, and role assignment
- [ ] Policy editor supports YAML editing with syntax validation
- [ ] System health page shows provider status, disk, and memory usage
- [ ] All admin actions are recorded in the audit log
