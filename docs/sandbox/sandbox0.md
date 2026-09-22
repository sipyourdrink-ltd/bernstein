# Sandbox0

Sandbox0 runs agent commands under stock gVisor and persists workspace files
using block-COW RootFS snapshots. Bernstein creates one Sandbox0 sandbox per
agent, transfers committed Git history and injected files, executes the agent
CLI, retrieves committed changes through a Git bundle, and deletes the sandbox.
The backend works with Sandbox0 Cloud or a self-hosted regional endpoint.

## Setup

Enabling this backend uploads the selected branch's **committed Git history**
and any explicit `FileEntry` content to the configured Sandbox0 endpoint. With
Sandbox0 Cloud, that data leaves the operator's infrastructure; with a
self-hosted endpoint, the deployment determines where it is stored. The Git
bundle includes history reachable from the selected tip, not just current
files: even previously deleted secrets can be in that history. Review the
repository and choose the endpoint accordingly. Sparse checkout limits the
checked-out files, not the history transferred in the bundle.

```shell
pip install 'bernstein[sandbox0]'
export SANDBOX0_API_KEY='<your key>'
export SANDBOX0_BASE_URL='https://api.sandbox0.ai'
export SANDBOX0_TEMPLATE='bernstein-agent'
bernstein agents sandbox-backends
bernstein run --sandbox sandbox0 --allow-paid
```

`SANDBOX0_TOKEN` is accepted as an alternative to `SANDBOX0_API_KEY`. The base
URL is optional; a self-hosted deployment should supply its regional API URL.
The template defaults to `default`. Configure a template containing `python3`,
Git, the chosen agent CLI and the project's build tools. Python is used to
preserve literal argv and binary stdin/stdout/stderr without shell interpolation
or the context API's bounded text log. The SDK is imported only when used.

`--allow-paid` is an explicit opt-in, including for a self-hosted endpoint.
Missing SDK/credentials or failed provisioning stops the run; it never launches
the agent on the host as a fallback. There is no stage-level `sandbox:` YAML
configuration for this backend; use the run flag or the Python interface.

Model credentials are separate from Sandbox0 credentials. For Claude-compatible
endpoints, the session receives only `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`
and `ANTHROPIC_BASE_URL`; for OpenAI-compatible CLI adapters it receives
`OPENAI_API_KEY` and `OPENAI_BASE_URL`. Provider credentials remain on the host.
Bernstein also copies the current agent's task-scoped token into `/tmp` with
mode 0600 and rewrites the prompt's token path. Tokens stay outside the Git
workspace and do not survive a RootFS snapshot.

The task server must be reachable from the remote sandbox. Configure
`BERNSTEIN_SERVER_URL` to a reachable authenticated endpoint before starting the
run. A host's loopback address is not reachable from the sandbox. Connectivity,
agent CLI compatibility and committed result retrieval must all be tested for
an end-to-end deployment; registration alone proves none of these.

## Python interface

```python
from bernstein.core.sandbox import WorkspaceManifest, get_backend

backend = get_backend("sandbox0")
session = await backend.create(
    WorkspaceManifest(root="/workspace", timeout_seconds=120),
    options={"template": "bernstein-agent"},
)
try:
    await session.write("hello.txt", b"hello")
    result = await session.exec(["cat", "hello.txt"])
    assert result.stdout == b"hello"
finally:
    await backend.destroy(session)
```

The backend declares `FILE_RW`, `EXEC`, `NETWORK` and `SNAPSHOT`. It does not
claim task-scoped mounts, GPUs or externally mounted persistent volumes.
Artifact mounts are rejected before a sandbox is allocated. Command timeout
and cancellation delete the remote execution context. Shutdown is idempotent;
a failed deletion remains retryable.

`GitRepoEntry` transfers the requested committed branch with `git bundle`.
Uncommitted/untracked host files and host Git credential configuration are not
uploaded; use `FileEntry` for deliberate file injection. A `branch="HEAD"`
manifest (including an orchestrator running from a detached checkout) exports
the current commit on a sandbox-local `bernstein-detached` branch, without
creating or changing host refs. Its commits return under
`refs/remotes/sandbox/<session>/bernstein-detached`. An unborn or invalid HEAD
fails explicitly; it does not switch to host execution. The template should
leave `/workspace` empty. Changes must be committed for Bernstein's existing
sync-back path to retrieve them. Sync-back is best effort in the shared spawner;
inspect `.sdd/runtime/sandbox/<session>.bundle` and
`refs/remotes/sandbox/<session>/*` before accepting a result.

## Snapshots and cleanup

`session.snapshot()` returns an opaque versioned reference to an immutable
RootFS snapshot. `backend.resume(reference)` claims a new sandbox from that
snapshot; it does not reconnect to the old running process. The workspace root,
base environment and command timeout are restored. Environment values are kept
inside the snapshot, not encoded in the reference. Treat snapshots as sensitive
when the workspace/environment contains credentials.

Specifically, **every value in `WorkspaceManifest.env` is written into RootFS
snapshot metadata** and restored with the snapshot. Reserve this field for
non-sensitive configuration. Do not put model API keys, task tokens or other
credentials there; pass those through `session.exec(..., env={...})` for each
command instead. Per-command overrides are not copied into snapshot metadata;
the adapter's temporary exec files live under `/tmp` and are cleaned up. This
does not prevent the command itself from writing credentials to persistent
files or logs. Encryption and mode 0600 do not expire a credential: deleting a
session preserves its snapshots, and rotation/revocation and snapshot deletion
remain the caller's responsibility.

The orchestrator currently uses an empty manifest environment and passes model
credentials per command; task-scoped tokens also stay under `/tmp`.

Automatic crashed-agent continuation through a host worktree is refused. Use
an explicit snapshot restore; uncommitted changes from a crashed runtime are
not recovered by Bernstein.

Snapshots retain filesystem state, not process memory, sockets or live REPLs.
Runtime-only paths such as `/tmp` do not persist. Destroying a session preserves
its snapshots so it can be restored after deletion or an orchestrator restart.
The caller owns snapshot retention: delete the provider snapshot identified by
`json.loads(reference)["snapshot_id"]` with
`client.sandboxes.delete_rootfs_snapshot(...)` when no longer needed.

The provider snapshot id is not a Bernstein CAS digest. The existing
CAS-specific `sandbox fork-race` receipt flow is not exposed for this backend.

## Validation

Install the `sandbox0` extra before running SDK tests. From the repository:

```shell
uv sync --extra sandbox0
uv run python scripts/run_tests.py tests/unit/sandbox/test_sandbox0_backend.py
CI_SANDBOX0_TEST=1 uv run python scripts/run_tests.py tests/integration/sandbox/test_sandbox0_backend.py
```

The live suite creates billable resources. Use a dedicated test region/team.
It runs the shared conformance contract plus binary stdin/output, literal argv,
cwd/permissions, cancellation/timeout side effects, independent snapshot
restores, and repository/file injection. Fixtures delete every created sandbox
and snapshot. A skipped suite is not live validation.


A separate opt-in test exercises the real Qwen Code CLI and a model endpoint,
agent-scoped token delivery, an authenticated callback, Git commit/bundle
retrieval, and sandbox deletion. Run it on a dedicated test host, with the
callback URL pointing back to that host at the configured port:

```shell
export OPENAI_API_KEY='<model key>'
export OPENAI_BASE_URL='<OpenAI-compatible endpoint>'
export SANDBOX0_AGENT_MODEL='<model>'
export SANDBOX0_TEST_CALLBACK_URL='http://<reachable-test-host>:18052'
export SANDBOX0_TEST_CALLBACK_PORT=18052
CI_SANDBOX0_AGENT_TEST=1 uv run python scripts/run_tests.py tests/integration/sandbox/test_sandbox0_agent.py
```

This test starts a temporary HTTP callback listener and makes billable model
calls. It exercises the production spawner path, not the full scheduler/task
server. The template must already contain Qwen Code; the test does not install
tools into a production template. Ordinary conformance tests make no model calls.
