# Adapter stream-signal protocol

A small, line-oriented vocabulary that any wrapped CLI can emit on its
stdout to participate in the same lifecycle that stream-json adapters
already expose: completion, question-asking, plan handoff, and blocked
state.

The protocol is **additive**. Adapters with a rich native event format
(Claude Code stream-json, Codex stream-json) keep their native shape;
the canonical signals are an optional overlay so non-stream-json CLIs
can join the same event bus without inventing a per-adapter event API.

## Grammar

Every canonical signal lives on a single line.

```
BERNSTEIN:<KIND>[ <json-object>]
```

Rules:

| Rule | Notes |
|---|---|
| Prefix | Literal `BERNSTEIN:` (case-sensitive, colon included). |
| Kind | One of the canonical kinds below, ALL-CAPS. |
| Separator | One space between kind and payload. |
| Payload | Optional JSON **object** (never an array, scalar, or `null`). |
| Encoding | UTF-8. Single line; no embedded newlines. |

Lines that don't match this grammar are treated as plain stdout and
ignored by the parser.

## Canonical kinds

| Kind | Terminal? | Payload shape |
|---|---|---|
| `COMPLETED` | yes | `{}` (or adapter-specific extras) |
| `FAILED` | yes | `{"reason": str, ...}` (optional) |
| `QUESTION` | no | `{"question": str, "options": list[str] \| null, "id": str \| null}` |
| `PLAN_DRAFT` | no | `{"markdown": str, "path": str \| null}` |
| `PLAN_READY` | no | `{"path": str}` (under `.sdd/`) |
| `BLOCKED` | no | `{"reason": str, "hint": str \| null}` |

Conformance checks expect at least one terminal signal (`COMPLETED` or
`FAILED`) per adapter run. Missing terminals surface as a
`MissingTerminalSignal` warning in the conformance report - not a
failure, so adapters that have not yet adopted the vocabulary keep
passing while the gap stays visible.

## Wrapper-script examples

### Bash

```bash
#!/usr/bin/env bash
set -euo pipefail

# Run the real CLI.
if my-cli "$@"; then
  printf 'BERNSTEIN:COMPLETED\n'
else
  rc=$?
  printf 'BERNSTEIN:FAILED {"reason":"exit %d"}\n' "$rc"
  exit $rc
fi
```

### Bash - ask a question mid-run

```bash
# Emit before blocking on operator input. The orchestrator routes the
# reply back via stdin (or whatever IPC channel the adapter uses).
printf 'BERNSTEIN:QUESTION %s\n' \
  '{"question":"Apply migration?","options":["yes","no"],"id":"q-001"}'
```

### Python wrapper

```python
from bernstein.core.protocols.stream_signals import SignalKind, format_signal

print(format_signal(SignalKind.PLAN_DRAFT, {"markdown": "# Plan\n- step 1"}))
print(format_signal(SignalKind.COMPLETED))
```

`format_signal` produces the same wire format as the shell snippet
above. The grammar is intentionally small enough that producers don't
need to share a library - Python adapters can use the helper, shell
wrappers can `printf` directly.

## Mapping a native protocol onto the canonical vocabulary

Adapters whose upstream CLI already emits structured events override
the `stream_signal_parser` method on `CLIAdapter` and translate native
events into `StreamSignal` instances:

```python
from bernstein.adapters.base import CLIAdapter
from bernstein.core.protocols.stream_signals import SignalKind, StreamSignal


class MyAdapter(CLIAdapter):
    def stream_signal_parser(self, line: str) -> StreamSignal | None:
        # Native protocol: one JSON object per line.
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Fall back to canonical grammar for downstream tools.
            return super().stream_signal_parser(line)
        if event.get("type") == "finish":
            return StreamSignal(
                kind=SignalKind.COMPLETED if event.get("ok") else SignalKind.FAILED,
                payload=event,
                raw_line=line,
            )
        return None
```

The default implementation already handles plain canonical signals, so
adapters with no native protocol don't need to override anything.

## Question round-trip

A `QUESTION` signal carries enough payload to route a reply back to the
correct in-flight question:

1. Adapter emits `BERNSTEIN:QUESTION {"question":"...", "options":[...], "id":"q-1"}`.
2. Orchestrator surfaces the question via the existing approval /
   elicitation path.
3. Operator answer is wrapped in `{"in_reply_to": "q-1", "answer": "..."}`
   and delivered back over the adapter's existing stdin/IPC channel.

The `id` field is optional. Adapters that never have more than one
question in flight may omit it; the orchestrator falls back to
first-in-first-out matching.

## Plan handoff

Adapters that produce planning artefacts emit two signals:

* `BERNSTEIN:PLAN_DRAFT {"markdown": "..."}` while the plan is still
  being refined.
* `BERNSTEIN:PLAN_READY {"path": ".sdd/plans/<slug>.md"}` once the
  markdown has been written to disk.

Downstream phases (`bernstein run <plan.yaml>`, subagent dispatch, etc.)
watch for `PLAN_READY` and pick up the file from `.sdd/`.

## Out of scope

* Replacing stream-json parsing for Claude or Codex.
* Binary or framed-byte protocols.
* Multi-line payloads (use a path-based handoff instead).
