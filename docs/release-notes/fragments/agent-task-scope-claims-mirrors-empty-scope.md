## Agent task scope covers server-chosen claims, versioned mirrors and empty scopes

Three gaps in how the task server bounds a task-scoped agent token are closed:

- `GET /tasks/next/{role}` and `POST /tasks/claim-receipt` pick the row for
  the caller, so no task id reached the scope check. Both now only consider
  tasks in the token's `task_ids`; a scoped token with nothing claimable in
  scope gets a 404 or a signed refusal receipt. A claim receipt granted to a
  scoped token records that scope in its `filter_digest`; unscoped digests are
  unchanged.
- The `/api/v<n>` mirror of a route now requires exactly the permission its
  root route does. Previously a mirror could fall through to a weaker default
  (for example `POST /api/v1/drain/cancel` needed `tasks:write` instead of
  `admin:manage`, and mirrored reads needed only `status:read`). A test pins
  the invariant for every route registered on both mounts.
- An empty `task_ids` claim is unrestricted only for the `manager` role (the
  run-root identity). A token of any other role with an empty list now reaches
  no task instead of every task. The identity store applies the same reading
  when a child identity is minted under a parent: a non-manager parent with an
  empty list can only mint a child with an empty list.

Operator-visible: tokens minted through the orchestrator are unaffected. A
hand-minted non-manager agent token with no task list, previously usable on
any task, is now refused on task writes; mint it with the task ids it needs,
or as `manager`. Agent tokens calling `/api/v1/...` mirrors now get the same
answer as on the root route.
