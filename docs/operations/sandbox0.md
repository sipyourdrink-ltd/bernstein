# Sandbox0 operations

See [Sandbox0 setup, credentials, snapshots and live validation](../sandbox/sandbox0.md).
Use an authenticated task-server endpoint reachable from the sandbox. Keep
provider credentials on the orchestrator and assign snapshot retention to an
operator: destroying a running session intentionally preserves its snapshots.

Choose Cloud versus self-hosted endpoints with the Git-history upload boundary
in mind. Keep secrets out of `WorkspaceManifest.env`, which is retained in
RootFS snapshots; pass short-lived command credentials via `exec(env=...)`.
Detached checkouts use a sandbox-local `bernstein-detached` branch so commits
remain retrievable without modifying host refs.
