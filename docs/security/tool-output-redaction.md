# Tool-output redaction (post-tool enforcement)

Bernstein inspects what a tool **produced** before that text reaches disk. The
check runs in the hook receiver's `PostToolUse` branch — the one place in the
live path where post-tool data is persisted — so the session sidecar, the
heartbeat and every downstream consumer see redacted text only.

Source: `src/bernstein/core/security/post_tool_enforcement.py`, wired from
`src/bernstein/core/server/hooks_receiver.py`.

## TL;DR

| | |
|---|---|
| Runs on | every `POST /hooks/{session_id}` whose event is `PostToolUse` |
| Reads | `tool_input` (or `input`) and `tool_response` / `tool_output` / `output` |
| Redacts | detected secrets in both, **before** truncation and before any write |
| Writes | an audit record to `.sdd/metrics/tool_audit.jsonl` |
| Blocks | a dangerous pattern writes `TOOL_ABORT` to the session's signals dir |

## What it looks for

Two independent pattern sets, both in `post_tool_enforcement.py`:

**Redacted** (replaced with the literal `[REDACTED]`, execution continues):
AWS access keys, GitHub tokens, PEM private-key headers, generic
`password`/`secret`/`api_key`/`access_token` assignments, SSNs, credit-card
numbers.

**Blocking** (execution is refused): output that looks like credential
exfiltration — `curl`/`wget`/`scp`/`rsync` aimed at a paste or tunnelling host.

## Where the output comes from

The hook adapter forwards the runner's body verbatim, so which key carries the
tool output is the runner's choice, not a Bernstein protocol. The receiver reads
the first of `tool_response`, `tool_output`, `output` that is present, and
serialises a structured value rather than dropping it — a secret in a nested
field must still be visible to the patterns. A runner that reports no output at
all does not disable enforcement: the tool input is still redacted and the audit
record is still written.

## What an operator sees

- `.sdd/runtime/hooks/{session_id}.jsonl` — the sidecar record, with
  `tool_input` and `tool_output` already redacted and then truncated to 200
  characters each. Redaction runs over the whole text first, so a secret
  straddling the cut cannot leave its first half behind.
- `.sdd/metrics/tool_audit.jsonl` — one line per `PostToolUse`, carrying the
  session, the tool, raw and redacted lengths, the count of secret patterns
  matched, and the `dangerous` / `blocked` flags. It records *that* a secret was
  found, never the secret.
- `.sdd/runtime/signals/{session_id}/TOOL_ABORT` — written through the same
  abort chain that owns every other per-tool refusal. The agent polls this file
  and can retry or skip; the session is not stopped.
- The hook response body reports `{"action": "tool_use_blocked"}` instead of
  `"tool_use_logged"` when the output was refused.

## Limitations

- **Regex-only, output-shaped.** The patterns are those of the pre-tool
  `check_secrets` flow applied to output text; a secret in a shape none of them
  match is not touched.
- **Scoped to the hook path.** Tool output that never reaches
  `POST /hooks/{session_id}` — a runner with no `PostToolUse` hook installed —
  is not covered by this mechanism.
- **Redaction, not prevention.** The tool has already run by the time the hook
  fires. The guarantee is about what is persisted and displayed, not about
  whether the secret was read.

## Related

- [Log redaction (PII filter)](log-redaction.md) — scrubs Python log records at
  the logging layer; a different sink, an overlapping pattern set.
- [PII scan quality gate](pii-scan-gate.md) — scans agent-produced diffs before
  merge. The pre-tool half of the same concern.
