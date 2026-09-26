# Sandbox0 backend

`core/sandbox/backends/sandbox0.py` implements `SandboxBackend` and
`SandboxSession` with the optional synchronous Sandbox0 Python SDK. Blocking
calls run in threads because the spawner uses separate event loops for create,
exec and teardown. Allocation calls are shielded so cancellation can reap the
returned resource instead of losing its id.

The registry discovers the backend without importing the SDK. The run-level
`--sandbox sandbox0` path attaches it with a workspace manifest factory;
provisioning failure never falls back to host execution. The sandbox is owned
by the spawn, including prompt-injection failure and crash-recovery routing.

Committed Git branches enter through bundles. Detached HEAD is staged as a
named ref in a temporary bare repository, preserving the exact source commit
and leaving host refs unchanged; the sandbox branch remains compatible with
the spawner's refs/heads-based result retrieval. Byte I/O uses SDK file APIs;
commands replace a Python launcher with literal argv and file-backed binary
stdio. Timeout/cancellation deletes the command context. Session deletion is
retryable and independent of snapshot retention. RootFS snapshots restore into
new sessions and carry workspace metadata inside the encrypted filesystem.

See [operator setup and limitations](../sandbox/sandbox0.md).
