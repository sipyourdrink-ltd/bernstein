# `agy` adapter - Antigravity CLI

Bernstein's adapter for the Antigravity CLI (`agy`), the successor CLI
for the hosted backend that the legacy `gemini` binary served on the
free / AI Pro / Ultra path. That backend was discontinued for
non-enterprise users in June 2026; operators on the default path who
stayed on the legacy binary discovered the cutoff as broken unattended
runs.

Verified against **Antigravity CLI 1.0.0**.

## Which lane am I in? (`agy` vs `gemini`)

Two adapters, two lanes -- pick by how you authenticate:

| Lane | Adapter | Binary | Who |
|---|---|---|---|
| Consumer / default | `agy` | `agy` | Free, AI Pro, Ultra subscribers (keyring / OAuth sign-in) |
| Enterprise / API key | `gemini` | `gemini` or `antigravity` | Enterprise licenses and paid API-key operators |

The `gemini` adapter (see `docs/adapters/gemini.md` and
`docs/adapters/antigravity.md`) remains fully supported for the
enterprise and API-key paths, including its dual-binary discovery
cascade. The `agy` adapter is a separate registry entry with its own
binary, contract, and conformance coverage, so the two lanes never
share discovery state.

```bash
# consumer lane
bernstein run --cli agy "fix the flaky test"

# enterprise / API-key lane
bernstein run --cli gemini "fix the flaky test"
```

## Invocation

The harness path is the CLI's print mode:

```
agy -p "<prompt>" --sandbox --dangerously-skip-permissions [--print-timeout <N>s]
```

* `-p` / `--print` -- run a single prompt non-interactively and print
  the response.
* `--sandbox` -- terminal-restriction sandbox, pinned on every spawn so
  unattended runs stay inside the workspace.
* `--dangerously-skip-permissions` -- auto-approve tool permission
  prompts so the run never blocks unattended.
* `--print-timeout` -- Bernstein mirrors its watchdog bound
  (`timeout_seconds`) onto the CLI's own print-mode timeout so both
  sides agree.

Model selection is server-side: the CLI exposes no model flag, so
Bernstein's `model` setting never reaches the argv for this adapter.

Session identifiers: the CLI mints its own conversation ids
(`--conversation <id>` resumes one). Native resume is not wired yet --
the strategy matrix declares resume as unsupported and the orchestrator
falls back to fresh sessions with scratchpad reinjection. The
`--conversation` flag is still pinned in the contract so an upstream
rename of the resume surface is caught before resume support lands.

## Install and auth

The Antigravity CLI is closed-source and distributed via an upstream
installer; Bernstein does not ship any installer URL inside the
package. Install from the upstream channel of your choice, then sign
in:

```bash
agy install     # configure environment paths and shell settings
```

Auth defaults to keyring / OAuth. `GOOGLE_API_KEY` / `GEMINI_API_KEY`
are honoured as optional overrides (shared with the gemini lane) and
pass through the adapter's env-isolation allow-list.

## Binary discovery

1. **Operator override.** If `BERNSTEIN_AGY_BINARY` is set and
   non-empty, that binary is used. If it does not resolve on `PATH` the
   adapter raises `AgyBinaryNotInstalledError`.
2. **Default.** The `agy` binary on `PATH`.

Implemented in `bernstein.adapters.agy.resolve_agy_binary`, covered by
`tests/unit/test_adapter_agy.py`.

## Health surfaces

* `bernstein adapters check agy` -- contract conformance verdict for
  the installed binary.
* `bernstein doctor` -- detects the binary, applies the version-floor
  advisory, and warns when the installed version is ahead of the
  canary's last-green version (see
  `docs/adapters/conformance-canary.md`).
