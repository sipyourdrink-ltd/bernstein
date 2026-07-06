# Proactive context compaction

Long-running workers eventually fill their context window. The **reactive**
lane only compacts *after* a worker has already crashed on an overflow, so
a worker always pays one failure before recovery. The **proactive** lane
compacts the task description - the mutable part of the prompt - when
context pressure crosses a configured threshold, before any overflow can
occur.

Every applied compaction is validated by zero-LLM invariant checks and
recorded as an HMAC-chained receipt. `bernstein compaction log` is the
operator surface over those receipts.

Source:

- `src/bernstein/core/orchestration/proactive_compaction.py` - the lane
- `src/bernstein/core/tokens/compaction_pipeline.py` - the pipeline
- `src/bernstein/core/tokens/compaction_receipt.py` - receipts
- `src/bernstein/core/tokens/sensitive_gate.py` - the credential gate
- `src/bernstein/cli/commands/compaction_cmd.py` - `compaction log`

## Enabling it

The proactive lane is **off by default**, so existing runs keep their
current behaviour. Enable it in the `compaction` block of `bernstein.yaml`:

```yaml
compaction:
  proactive: true      # master switch (default: false)
  threshold: 0.8       # context-window fraction to compact at (default: 0.8)
  max_per_task: 1      # max proactive compaction attempts per task (default: 1)
```

| Key | Default | Meaning |
|-----|---------|---------|
| `proactive` | `false` | Master switch for the proactive lane. |
| `threshold` | `0.8` | Context-window fraction, in `(0, 1]`, at which to compact. Out-of-range values fall back to the default. |
| `max_per_task` | `1` | Maximum proactive compaction attempts per task. |

When a worker's context utilization crosses `threshold`, the task
description is compacted, validated, receipted, and patched onto the task
server.

## Invariants

- **Validators gate the summary.** The compacted summary never reaches the
  worker unless every zero-LLM validator passes - code blocks and error
  text must survive. One fix-only retry is allowed, then the compaction
  aborts. An aborted proactive compaction changes nothing.
- **The reactive lane is the untouched fallback.** The proactive
  meta-message deliberately avoids the marker the reactive lane counts, so
  a proactive compaction never consumes the reactive retry budget.
- **Every applied compaction is receipted:** a `compaction.receipt` event
  in the HMAC audit chain, a replay-journal step carrying the pre/post
  hashes, a zero-cost spend-ledger row, and a metric point.

## The credential-shaped-content gate

The compaction pipeline forwards slices of worker context to a model API
to produce summaries, and worker context routinely contains file reads. A
context that included `.env` contents, a private key, or a cloud
credential would otherwise be shipped to an external API. The sensitive
gate scans compaction input **before** the summary stage.

- **Redact when the span is delimitable, refuse otherwise.** Content with
  a well-defined span (a complete PEM block, an AWS access key id, a
  `ghp_` token) is replaced with a typed placeholder
  `[REDACTED:<rule-id>:<hash>]`. Path-shaped tokens (`.env`, `id_rsa`,
  `*.pem`, `.ssh/`) signal that credential *file contents* may surround the
  token in ways the gate cannot delimit, so any such hit refuses the whole
  compaction. An unterminated PEM header also refuses.
- **Hash only, never content.** Findings and audit events carry the
  SHA-256 of the offending span; the span text itself is never stored.
- **Reactive-path refusals fail fast** with a typed reason instead of
  burning retries.

The gate is on by default. It is configured in the same `compaction`
tuning block:

```yaml
compaction:
  sensitive_gate_enabled: true        # default: true
  sensitive_gate_extra_deny:          # extra regex deny patterns (redacted on hit)
    - "MYCORP_[A-Z0-9]{32}"
  sensitive_gate_allow:               # suppress specific findings (audit-logged)
    - "content.aws-access-key:1a2b3c4d"
```

| Key | Default | Meaning |
|-----|---------|---------|
| `sensitive_gate_enabled` | `true` | Master switch for the gate. |
| `sensitive_gate_extra_deny` | `[]` | Extra operator-supplied deny regexes; hits are redacted with a typed placeholder. |
| `sensitive_gate_allow` | `[]` | Allowlist entries (`rule-id` or `rule-id:hash8`). Suppressions are audit-logged. |

An allowlist entry is either a rule id (`content.aws-access-key`) or a rule
id plus the first 8 hex chars of the span hash
(`content.aws-access-key:1a2b3c4d`).

## `bernstein compaction log`

Prints and verifies a task's compaction receipt chain.

```bash
bernstein compaction log --task <id>
bernstein compaction log --task <id> --json
bernstein compaction log --task <id> --verify
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--task ID` | required | Task whose compaction receipts to print. |
| `--audit-dir DIR` | `.sdd/audit` | Audit chain directory holding the receipt events. |
| `--sdd-dir DIR` | `.sdd` | Run-state dir; replay journals live under it. |
| `--json` | off | Structured output. |
| `--verify` | off | Re-verify the chain and cross-check replay-journal compaction steps; exit 1 on failure. |

The table shows, per receipt: timestamp, trigger, tokens before/after,
validator results, retry count, gate action, the truncated pre/post
SHA-256 pair, and the correlation id.

`--verify` walks each per-agent replay journal, cross-checks its
compaction steps against the chain receipts, and exits non-zero when any
journaled compaction step lacks a chain-verifiable receipt or when the
receipt hashes disagree with the journal. This is the same check the
run's audit verification applies.

## Related

- [Tiered context compaction](../architecture/memory_tiers.md)
- [Audit log (operator guide)](../security/audit-log.md)
- [Deterministic replay](deterministic-replay.md)
