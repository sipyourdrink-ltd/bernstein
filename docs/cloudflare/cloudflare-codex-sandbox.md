# Codex on Cloudflare Sandboxes

Run Codex inside a Cloudflare sandbox container instead of on the orchestrator
host, by driving the sandbox bridge Worker you deploy into your own account.

**Module:** `bernstein.adapters.codex_cloudflare`
**Class:** `CodexCloudflareAdapter`

!!! info "Implementation status"
    Implemented against the HTTP bridge published by
    **`@cloudflare/sandbox` 0.12.4**, which serves **API contract version
    `1.0.0`** at `GET /v1/openapi.json`.

    **End-to-end verification against a live deployment is pending.** The
    adapter is built and tested against the published bridge contract, with
    recorded HTTP and SSE fixtures that mirror the documented payloads. It has
    **not** been run against a real Cloudflare deployment. If you have a
    Workers Paid account, the [live verification](#live-verification) command
    below is the one-shot check — please report what you find.

---

## What it does

| Step | Bridge route |
|---|---|
| Create sandbox | `POST /v1/sandbox` → `{"id": ...}` |
| Seed workspace | `POST /v1/sandbox/:id/hydrate` (tar) or `PUT /v1/sandbox/:id/file/*` |
| Open session (cwd + env) | `POST /v1/sandbox/:id/session` → `{"id": ...}` |
| Run the agent | `POST /v1/sandbox/:id/exec` → SSE, `Session-Id` header |
| Collect the workspace | `POST /v1/sandbox/:id/persist` → tar → diff |
| Stop and tear down | `DELETE /v1/sandbox/:id` |
| Pool bookkeeping | `GET /v1/pool/stats` |

`stdout` and `stderr` arrive as base64 SSE frames and are decoded and handed to
your `on_output` callback as they stream, not buffered to the end. The stream
terminates on `exit` (`{"exit_code": N}`) or `error` (`{"error": ..., "code": ...}`).

---

## Prerequisites

1. **A Cloudflare Workers Paid plan** ($5/month). Containers — which sandboxes
   are built on — cannot be deployed on the free plan.
2. **Docker running locally** and Node.js ≥ 16.17, to build and push the
   container image at deploy time.
3. **An operator-deployed bridge Worker.** Bernstein does not host it and
   cannot deploy it for you; it lives in your account and bills to it.

---

## Operator deploy steps

```bash
# 1. Scaffold the bridge Worker from the upstream template.
npm create cloudflare -- sandbox-bridge --template=cloudflare/sandbox-sdk/bridge/worker
cd sandbox-bridge

# 2. Pin the SDK version this adapter implements against.
npm install @cloudflare/sandbox@0.12.4

# 3. Set the shared secret. THIS STEP IS NOT OPTIONAL - see the warning below.
npx wrangler secret put SANDBOX_API_KEY

# 4. Deploy.
npx wrangler deploy
```

Then point Bernstein at it:

```python
from bernstein.adapters.codex_cloudflare import CodexCloudflareAdapter, CodexSandboxConfig

adapter = CodexCloudflareAdapter(
    CodexSandboxConfig(
        bridge_url="https://sandbox-bridge.<your-subdomain>.workers.dev",
        bridge_api_key="<the SANDBOX_API_KEY you just set>",
        openai_api_key="<key the codex CLI needs inside the sandbox>",
    ),
)
```

### Instance type: the default is too small for a coding agent

The upstream template ships `"instance_type": "lite"`, which is **1/16 vCPU,
256 MiB RAM, 2 GB disk**. That is enough to run `echo`; it is not enough to run
a coding agent that clones a repo, installs a toolchain, and runs a test suite.

Set the instance type in your Worker's `wrangler.jsonc` before deploying:

```jsonc
{
  "containers": [
    {
      "class_name": "Sandbox",
      "image": "./Dockerfile",
      "instance_type": "standard-3",  // 2 vCPU, 8 GiB RAM, 16 GB disk
      "max_instances": 5              // raise if you run sandboxes concurrently
    }
  ]
}
```

| Instance type | vCPU | Memory | Disk |
|---|---|---|---|
| `lite` (template default) | 1/16 | 256 MiB | 2 GB |
| `basic` | 1/4 | 1 GiB | 4 GB |
| `standard-1` | 1/2 | 4 GiB | 8 GB |
| `standard-2` | 1 | 6 GiB | 12 GB |
| `standard-3` | 2 | 8 GiB | 16 GB |
| `standard-4` | 4 | 12 GiB | 20 GB |

**`standard-3` or larger is the realistic floor for a coding agent.** Sizing is
a deploy-time decision: the bridge exposes no per-request memory or CPU
control, which is why the adapter has no such setting (see
[retired settings](#settings-that-no-longer-exist)).

The container image must also contain the `codex` CLI. Add it to the
Dockerfile you deploy — the stock image does not include it.

---

## The authentication warning

!!! danger "A bridge with no `SANDBOX_API_KEY` is open to the internet"
    The bridge's auth checks are **conditional on the secret being set**. With
    `SANDBOX_API_KEY` unset, both the `/v1/sandbox/*` middleware and the inline
    check on `POST /v1/sandbox` pass every request through — anyone who finds
    the URL can create containers, run commands, and read files, billed to your
    account.

    `CodexCloudflareAdapter.preflight()` therefore probes `POST /v1/sandbox`
    with no `Authorization` header. If that call **succeeds**, preflight deletes
    the sandbox it just created and raises
    `CodexCloudflareBridgeAuthError` rather than using the deployment. Run
    preflight before your first real run.

Preflight also reads `GET /v1/openapi.json` and:

- refuses a bridge whose API contract **major** differs from `1.0.0`
  (`CodexCloudflareBridgeVersionError` — opt out with
  `require_supported_api_version=False`);
- refuses a bridge whose published `paths` are missing any route the adapter
  drives (`CodexCloudflareBridgeContractError` — always strict).

The route check is the one that matters in practice: `info.version` is the
hand-maintained API contract version, **not** the npm package version, and it
can stay at `1.0.0` while routes move underneath it. The package has already
removed transports across releases.

---

## Cancellation

Closing the `/exec` SSE stream **does not stop the remote process.** The
container keeps running the command and keeps billing. Cancellation is the
explicit `DELETE /v1/sandbox/:id`, which `CodexCloudflareAdapter.cancel()`
issues; `execute()` also issues it from a `finally`, so a failed or cancelled
run tears its sandbox down.

`cancel()` deliberately does **not** confirm with
`GET /v1/sandbox/:id/running`. That route sits behind the bridge's warm-pool
middleware, which acquires — and if necessary **starts** — a container for the
id before the handler runs `true` inside it. Probing it after a delete would
report `running: true` on a perfectly healthy bridge *and* start a fresh
billable container, undoing the cancellation. Teardown is confirmed against
`GET /v1/pool/stats`, which reads pool state without allocating, and the result
is returned on `CancelOutcome.pool_after`.

`is_running()` and `get_status()` remain available for asking about a sandbox
you intend to keep using — but they carry the same allocation caveat.

---

## Sandbox evidence

A remote run lands the same signed evidence as a local one. `execute()` hashes
the exact tar returned by `/persist` into a content address and records it on
`CodexSandboxResult.evidence`:

```python
result = await adapter.execute(prompt, workspace_id, workspace_tar=seed)
candidate = result.evidence.to_race_candidate("task-1", {"correctness": 1.0})
# -> bernstein.core.sandbox.selection_receipt.RaceCandidate
```

The candidate carries `terminal_snapshot_digest` (SHA-256 of the workspace tar,
re-hashable offline) and `isolation = "requested:codex-cloudflare"`, so it signs
into a selection receipt alongside locally-produced candidates and a verifier
can see *where* the task ran. The `requested:` prefix matches the convention in
`bernstein.core.sandbox.fork_race`: the adapter asked a remote bridge to run the
work, it did not boot or probe the isolation boundary, so it does not claim to
attest the posture. What it does attest — the content-addressed digest — is
verifiable without the network.

---

## Live verification

If you have a Workers Paid account and a deployed bridge, this is the one
command that exercises the full path — preflight, seed, stream, diff, evidence,
and teardown — against your real deployment:

```bash
BRIDGE_URL="https://sandbox-bridge.<your-subdomain>.workers.dev" \
BRIDGE_API_KEY="<your SANDBOX_API_KEY>" \
uv run python - <<'PY'
import asyncio, io, os, tarfile
from bernstein.adapters.codex_cloudflare import CodexCloudflareAdapter, CodexSandboxConfig

def seed() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"before\n"
        info = tarfile.TarInfo("./probe.txt"); info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()

async def main() -> None:
    adapter = CodexCloudflareAdapter(
        CodexSandboxConfig(
            bridge_url=os.environ["BRIDGE_URL"],
            bridge_api_key=os.environ["BRIDGE_API_KEY"],
            agent_command=("sh", "-c"),   # stand in for `codex` so no model key is needed
        ),
    )
    print("preflight:", await adapter.preflight())
    result = await adapter.execute(
        "echo hello && echo after > /workspace/probe.txt",
        "live-check",
        model="ignored",
        timeout_minutes=2,
        workspace_tar=seed(),
        on_output=lambda stream, chunk: print(f"[{stream}] {chunk.decode(errors='replace')}", end=""),
    )
    print("status:", result.status, "exit:", result.exit_code)
    print("files_changed:", result.files_changed)
    print("evidence:", result.evidence)
    print("cancel:", await adapter.cancel(result.sandbox_id))

asyncio.run(main())
PY
```

Expected: preflight reports `auth_enforced=True` and `api_version='1.0.0'`;
`status: completed exit: 0`; `files_changed: ['probe.txt']`; a 64-hex
`terminal_snapshot_digest`; and a cancel outcome whose `pool_after.assigned`
does not count the sandbox.

`agent_command=("sh", "-c")` keeps the check independent of whether your image
carries the `codex` CLI. With the real agent installed, drop that override and
the adapter runs `codex exec --model <model> <prompt>`.

!!! warning "This costs money"
    The command starts a real container on your paid account. It tears it down
    at the end, but a crashed run can leave one behind — check the Cloudflare
    dashboard, or `DELETE /v1/sandbox/:id`, if you interrupt it.

---

## Limitations

These are properties of the bridge, stated plainly rather than papered over.

**No egress restriction.** The bridge applies none, and exposes no domain
allowlist. Code running in the sandbox can reach the internet. If you need
network containment, the sandbox is not where you get it.

**Symlink escape caveat.** The bridge resolves request paths within
`/workspace` and rejects traversal, but a *symlink created inside the
workspace* can still name a path outside it, and the persist archive is built
by `tar` from that tree. Bernstein never extracts the returned tar — it only
reads member contents to compute the diff and the digest — so an escaping
symlink cannot become a write on your host. Extract a persisted workspace
yourself only with a tar reader you trust to refuse absolute and traversing
member paths.

**32 MiB payload cap.** Both `PUT /v1/sandbox/:id/file/*` and
`POST /v1/sandbox/:id/hydrate` reject payloads over 32 MiB. The adapter checks
locally first and raises `CodexCloudflarePayloadTooLargeError` with the size,
rather than letting you discover it as an opaque rejection. Trim the seed with
`persist_excludes`, or clone large inputs from inside the sandbox.

**Sessions do not survive container sleep.** The adapter opens one session per
run for `cwd` and environment. A container that sleeps mid-run loses it.

**No remote log store.** Output exists only on the `/exec` SSE stream while the
command runs. `get_logs()` returns the transcript this adapter buffered — it
does not fetch anything back from Cloudflare.

**Unconfigured means refused.** Without `bridge_url` and `bridge_api_key`,
every method raises `CodexCloudflareNotConfiguredError`. The adapter never
falls back to running the agent on the orchestrator host — if you want that,
use the `codex` adapter deliberately.

---

## Configuration

`CodexSandboxConfig` fields:

| Field | Type | Default | Description |
|---|---|---|---|
| `bridge_url` | `str` | `""` | Base URL of your deployed bridge Worker |
| `bridge_api_key` | `str` | `""` | The Worker's `SANDBOX_API_KEY`, sent as Bearer |
| `openai_api_key` | `str` | `""` | Key the agent CLI needs inside the sandbox |
| `workdir` | `str` | `"/workspace"` | Workspace root inside the sandbox |
| `agent_command` | `tuple[str, ...]` | `("codex", "exec")` | argv prefix; `--model` and the prompt are appended |
| `extra_env` | `tuple[tuple[str, str], ...]` | `()` | Extra session environment pairs |
| `max_execution_minutes` | `int` | `30` | Default run ceiling, sent as `timeout_ms` |
| `request_timeout_seconds` | `float` | `60.0` | HTTP timeout for control-plane calls |
| `persist_excludes` | `tuple[str, ...]` | `(".git",)` | Paths dropped from the persisted tar |
| `require_supported_api_version` | `bool` | `True` | Refuse a different API contract major |

The agent's API key travels on the **session**, not in argv: the bridge's
`ExecRequest` schema is exactly `{argv, timeout_ms, cwd}` with no `env` field,
and keeping the key out of argv also keeps it out of the sandbox process table
and the shell command string the bridge assembles.

### Settings that no longer exist

Building against the real bridge retired settings it cannot honour. They are
**absent**, not ignored, and `CodexSandboxConfig.from_mapping()` rejects each
by name with the reason — so an old config produces an explanation rather than
silent no-ops.

| Retired setting | Why the bridge cannot honour it |
|---|---|
| `memory_mb`, `cpu_cores` | Container sizing is the deploy-time wrangler `instance_type` |
| `network_access` | The bridge applies no egress restrictions and has no allowlist |
| `sandbox_image` | The image is the deploy-time wrangler `image` |
| `r2_bucket` | Bucket mounts need a deploy-time R2 binding; transfer uses hydrate/persist |
| `cloudflare_account_id`, `cloudflare_api_token` | The bridge authenticates with its own secret |
| `tokens_used` (result) | The bridge reports exit codes and output, never token counts |

```python
CodexSandboxConfig.from_mapping({"memory_mb": 1024})
# CodexCloudflareConfigError: ... `memory_mb`: container memory is a deploy-time
# wrangler setting (`containers[].instance_type`); it cannot be sized per request.
```

---

## Errors

| Error | Raised when |
|---|---|
| `CodexCloudflareNotConfiguredError` | No `bridge_url` / `bridge_api_key` |
| `CodexCloudflareConfigError` | Retired or unknown config key |
| `CodexCloudflareBridgeAuthError` | Bridge serves `/v1/sandbox` unauthenticated |
| `CodexCloudflareBridgeVersionError` | Served API contract major differs |
| `CodexCloudflareBridgeContractError` | A required route is missing from the OpenAPI document |
| `CodexCloudflareBridgeApiError` | Any bridge route returned non-2xx |
| `CodexCloudflarePayloadTooLargeError` | Upload over the 32 MiB cap |
| `CodexCloudflareCancelError` | The delete that stops the container was refused |
