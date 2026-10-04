## Workflow commands need a trusted workspace; claims are role-bound; receipt keys must be pinned; worktree and skill paths are contained

Five hardening changes in one set:

- **Workflow and hook commands require a trusted workspace.** `WorkflowRunner`
  command nodes, `when` / `loop.until` predicates and script hooks run
  repository-authored text in a subprocess. They now check
  `is_workspace_trusted(workdir)` first, the rule the plugin manager already
  applies to committed hook scripts. In an untrusted workspace a command node
  fails with a "not trusted" error and a `workflow.untrusted_workspace` audit
  event; a predicate raises `UntrustedWorkspaceError`; a script hook raises
  `hooks.UntrustedWorkspaceError`. In-process callable hooks are not gated.
- **Task claims and role edits are bound to the agent token's role.** With an
  agent identity, the role comes from the token, not the request:
  `POST /tasks/{id}/claim` answers 403 on a role mismatch, `claim-batch` skips
  tasks of another role, `GET /tasks/next/{role}` answers 403 when the path
  role differs from the token role, the re-claim inside `/complete` and `/fail`
  is role-bound too, and `PATCH /tasks/{id}` accepts only the token's own role
  on a task already in that role. Operator, SSO and cluster credentials keep
  full control.
- **Receipt verification fails without a pinned key.** `bernstein-verify-receipt`,
  `tools/verify_audit_receipt.py` and `bernstein audit receipt verify` now
  FAIL the `public_key` check and exit non-zero when the signing key is not
  pinned (`--trust-anchor` / trusted key). `--allow-unpinned-key` keeps the old
  integrity-only result as `OVERALL: PASS (unpinned key: integrity only)`; the
  key-trust status is always printed. The lineage pillar of `bernstein audit
  verify` fails on an inactive or revoked lineage entry and fails closed when
  the chain cannot be read; the revocation reader now reads
  `lineage_entry_hash` / `lineage_ref` where the writer puts them.
- **Worktree session ids are validated and contained.** `WorktreeManager.create()`
  validates the session id and requires the resolved target to be a direct
  child of `.sdd/worktrees` before any git or filesystem action, which also
  covers a symlink already present at the target.
- **Skill names are slugs, and installs stay inside the skills root.** A catalog
  entry `name` must fully match `[a-z][a-z0-9-]*`; `install_local` and
  `remove_skill` resolve the install and staging directories and require them
  to be direct children of the skills root before anything is created,
  replaced or deleted.

**Behaviour changes.** A script or pipeline that verifies receipts without
pinning the signing key now exits non-zero: pin the key with `--trust-anchor`,
or pass `--allow-unpinned-key` to keep the integrity-only result. `bernstein
audit verify` also fails on a chain holding inactive or revoked lineage
entries. Workflows with command nodes, predicates or script hooks need a
trusted workspace (`bernstein trust`), as plugin hooks already did.
