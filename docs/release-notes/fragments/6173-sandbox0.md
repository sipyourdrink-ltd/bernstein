## Sandbox0 remote agent execution and RootFS snapshots

Add the optional Sandbox0 backend for agent CLI execution in gVisor sandboxes,
with file and Git injection, detached checkout support, committed-work retrieval,
and caller-retained RootFS snapshots. Enable it explicitly with the Sandbox0
extra, provider credentials, and `--sandbox sandbox0 --allow-paid`. Snapshot
restore preserves files, not running processes; review the Sandbox0 setup guide
for repository upload and credential retention boundaries (#6173).
