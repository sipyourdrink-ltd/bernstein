# `muse` adapter - Muse Code

Bernstein's adapter for Muse Code, Meta's terminal coding agent
([vendor docs](https://dev.meta.ai/docs/muse-code)). Muse Code ships a
non-interactive mode intended for scripts and CI, which is exactly the
execution contract Bernstein wraps: a Muse Code run gets the same
worktree isolation, journaling, and receipts as every other
adapter-managed worker.

Last verified against the vendor docs on **2026-08-10**.

## Install and auth

```bash
curl -fsSL https://dev.meta.ai/install.sh | sh
```

Static binary for macOS/Linux (x86_64/arm64); no native Windows support
(the vendor's documented route there is WSL2). Interactive use signs in
via a browser device-code flow; unattended runs authenticate with the
`META_API_KEY` environment variable, which is the only credential the
adapter passes through the filtered worker environment.

```bash
export META_API_KEY=...
bernstein run --cli muse "fix the flaky test"
```

## Invocation

The harness path is the CLI's headless mode:

```
muse --model <id> --disable-approval exec "<prompt>"
```

* `exec` - run a single prompt to completion, non-interactively.
* `--model` - model selection; defaults to `muse-spark-1.2`, the
  vendor's documented default.
* `--disable-approval` - skips approval prompts so the run never blocks
  unattended, while keeping the CLI's own sandbox containment. The
  stronger `--yolo` flag also disables that sandbox and is deliberately
  not used.

Version probe: `muse --version`.

## Model handling

The vendor lineup is a single model family today, so the adapter keeps
model routing strict:

| Input | Result |
|-------|--------|
| (empty) | `muse-spark-1.2` |
| `spark` | `muse-spark-1.2` |
| `muse-spark-1.2` (or any `muse-*` id) | passed through unchanged |
| anything else (e.g. `sonnet`) | fails loudly with `ValueError` |

A foreign logical name reaching this adapter is a routing mistake worth
surfacing, not something to silently remap onto the default.

## Not wired up (verified upstream, unused at spawn time)

* `--json` - headless JSONL event stream; the adapter reads plain text
  output through the standard text-signal channel.
* `--session-id <uuid>` - resumes an existing session non-interactively;
  resume stays declared unsupported until a spawn path supplies one, so
  retry falls back to a fresh spawn with scratchpad reinjection.
* `--prompt-file`, `--max-model-steps`, `--no-session-log` - headless
  flags the adapter does not currently need.

See `src/bernstein/adapters/muse.py` for the adapter source and
`tests/contract/contracts/muse.yaml` for the pinned CLI surface.
