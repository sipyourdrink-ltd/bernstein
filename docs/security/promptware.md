# Promptware cross-agent C2 detection

Bernstein ships an inline detector for `promptware` payloads embedded in
tool output. The threat model is the public Agent Commander (16 Mar 2026)
finding: an attacker plants natural-language tasking inside a web page,
email, or repository file that a Bernstein agent fetches via a tool call.
A downstream agent then reads that tool output, interprets the embedded
instructions, and acts on behalf of the attacker. Twelve in-the-wild
cases were documented by Palo Alto Unit 42 as of March 2026.

The orchestrator is the only layer that can see the cross-agent edge,
which is why detection lives here and not in a single-agent runner.

## What runs in the hot path

* Module: `bernstein.core.security.promptware_detector.PromptwareDetector`
* Entry point: `PromptwareDetector.classify(text: str) -> PromptwareScore`
* Wiring: `bernstein.core.security.promptware_ingest.scan_tool_output(...)`

The classifier is regex bag matching plus URL-density and command-token
features, fed into a Bayesian update against a prior keyed by output-size
bucket (`tiny`, `small`, `medium`, `large`). The hot path has no LLM
call, no I/O, and no global locks; it is safe to run on every tool result.

## Score bands

| Band       | Range            | Action                                                    |
|------------|------------------|-----------------------------------------------------------|
| benign     | `score <= 0.7`   | no action; histogram observation only                     |
| suspicious | `0.7 < score <= 0.9` | `WARN` log line with task id, adapter, tool, source URL |
| malicious  | `0.9 < score`    | lifecycle event so plugins can subscribe-and-abort        |

`PromptwareScore.is_warn` and `.is_abort` are the canonical predicates -
callers should not branch on raw thresholds.

## Feature flag and rollout

The detector is opt-in until precision is measured in production.

```
export BERNSTEIN_PROMPTWARE_DETECTOR=on
```

Accepted truthy values: `on`, `1`, `true`, `yes` (case-insensitive). The
flag is read on every classification, so flipping it does not require a
restart. When the flag is off, `scan_tool_output` returns a zero-score
placeholder so call sites can emit uniform structured logs.

## Subscribing to the abort signal

The detector emits a structured `post_task` lifecycle event whose
payload carries `promptware_abort: true` and a `promptware` block with
the full score dictionary. A Bernstein plugin (or a Python callable
registered with `HookRegistry.register_callable`) can react and raise
`HookFailure` to refuse the next-agent spawn:

```python
from bernstein.core.lifecycle.hooks import HookFailure, LifecycleContext, LifecycleEvent

def abort_on_promptware(ctx: LifecycleContext) -> None:
    if ctx.data.get("promptware_abort"):
        raise HookFailure(
            ctx.event,
            "abort:promptware",
            exit_code=1,
            stderr="promptware detected; refusing to spawn next agent",
        )

registry.register_callable(LifecycleEvent.POST_TASK, abort_on_promptware)
```

`scan_tool_output` swallows plugin exceptions on the abort path so a
buggy plugin can never break the orchestrator. Plugins that need to
guarantee an abort should also persist a sentinel in their own state
store and re-check at the next decision point.

## Telemetry

Histogram: `bernstein_security_promptware_score{adapter,tool,bucket}`

Buckets: `0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.8, 0.9, 0.95, 1.0`. Adapter
and tool labels are sanitised: values outside `[A-Za-z0-9_.-]{1,32}`
collapse to `other` to bound cardinality. The bucket label collapses to
`unknown` for any value not in the closed `tiny/small/medium/large` set.

## False-positive expectations

The v1 detector is tuned for the published corpus:

* AUROC on the in-repo corpus is `1.0` (20 benign + 5 promptware).
* Single-pattern hits in tiny outputs can reach the warn band; that is
  deliberate so that a short web page containing a single imperative
  prompt does not slip through.
* Operator log scrapers that quote the literal string "ignore previous"
  will trip warn. Add a project allowlist or wrap the scraper output in
  a non-promptware-bearing channel.

## CLI

```
bernstein doctor promptware-scan <run-id> [--threshold 0.7] [--json]
```

Replays `.sdd/traces/<run-id>.jsonl` and prints rows whose score reaches
the threshold. Exits non-zero if any row reaches the abort band, which
lets you pipe the command into CI.

## Out of scope (v1)

* Sanitising tool input before the agent reads it. The model already
  chose to call the tool; the integrity boundary is the tool output.
* Cross-agent credential isolation. Covered by
  `bernstein.core.credential_scoping`.
* An LLM-as-judge classifier. Tracked for v2 once v1 precision is
  measured in production.
