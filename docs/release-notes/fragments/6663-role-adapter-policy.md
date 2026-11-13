Saved role-to-adapter restrictions now apply to initial dispatch, cached
adapters, crash recovery and provider failover. The spawner checks before
workspace preparation and again before dispatch, using its project-root policy
even when resuming a nested worktree. Container, sandbox and bridge dispatch
also check the policy. Forbidden provider-chain candidates are refused before
health probes.

A present but unusable policy now stops dispatch with a reason instead of
silently allowing all adapters. This includes directories, unreadable or
malformed files, invalid UTF-8, and role values that are not lists of strings.
Missing files and explicitly empty lists keep their existing unrestricted
meaning. Refusals record the role, adapter and reason in the local audit chain;
an audit failure still refuses the adapter and logs a warning.

Partial implementation of #6648: this governs AgentSpawner, not direct adapter
API calls or agents already running.
