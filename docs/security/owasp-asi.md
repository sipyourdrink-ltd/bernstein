# OWASP ASI01-10 detector pack

Bernstein ships ten heuristic detectors aligned to the OWASP Top 10
for Agentic Apps (ASI01-10, December 2025). The pack runs as a single
`OwaspAsiGuardrail` inside the `GuardrailPipeline`, scanning prompt
and tool-call envelopes for the canonical agentic-app risk classes.

Source: `src/bernstein/core/security/owasp_asi_detectors.py`.

---

## Detector inventory

| Code | Risk class | Status |
|------|------------|--------|
| ASI01 | Goal Hijack - lexical scan for "ignore previous instructions" patterns | heuristic |
| ASI02 | Tool Misuse - args contain shell tokens for non-shell tools | heuristic |
| ASI03 | Identity & Privilege Abuse - capability-matrix delegation | delegating |
| ASI04 | Agentic Supply Chain - unsigned MCP / plugin / skill load | delegating |
| ASI05 | Unexpected Code Execution - `eval`/`exec`/shell-shaped tool args | heuristic |
| ASI06 | Memory Poisoning - append-only log integrity drift | heuristic |
| ASI07 | Insecure A2A - unsigned agent cards in A2A traffic | delegating |
| ASI08 | Unbounded Consumption - token / cost / loop budget breach | heuristic |
| ASI09 | Observability Gap - missing audit-chain entry for a tool call | heuristic |
| ASI10 | Misalignment Drift - stated intent vs imminent action | deferred |

The `status` field is honest: `heuristic` is a working pattern check,
`delegating` defers to a deeper module when the caller populates the
relevant context key, and `deferred` is a placeholder that returns
`INFO` until the deeper integration ships.

Each detector consumes a uniform `context` dict and returns an
`ASIFinding` with `severity` of `info` / `warning` / `critical`. The
guardrail aggregates findings into a single `GuardrailResult` and
blocks the call when any finding meets the configured `block_on`
threshold (default: `warning`).

---

## Default-on resolution

The pack is on by default. Resolution order (highest priority first):

1. `BERNSTEIN_DISABLE_OWASP_ASI=1` - pack disabled.
2. `BERNSTEIN_ENABLE_OWASP_ASI=0` (or `false`/`no`/`off`) - pack
   disabled. The legacy opt-in flag stays honoured so a falsy value
   suppresses the pack for operators who scripted the older semantics.
3. Otherwise - pack enabled.

The toggle is read by `is_owasp_asi_enabled()` and consulted by
`GuardrailPipeline.default()`:

```python
from bernstein.core.security.guardrail_pipeline import GuardrailPipeline

pipeline = GuardrailPipeline.default()  # respects env
pipeline = GuardrailPipeline.default(enable_owasp_asi=True)  # force on
pipeline = GuardrailPipeline.default(enable_owasp_asi=False)  # force off
```

Detector load failures are caught and logged; the pipeline keeps
running with the existing guardrails so a faulty detector never blocks
the entire orchestrator.

---

## Context envelope

Each detector documents the keys it reads. The full set the orchestrator
populates today:

| Key | Purpose |
|-----|---------|
| `prompt` | User / system prompt, scanned by ASI01 + ASI05 |
| `retrieved_content` | RAG context, scanned by ASI01 + ASI06 |
| `system_prompt` | System message, scanned by ASI01 |
| `tool_name` | Tool about to be invoked |
| `tool_args` | Tool argument dict, scanned by ASI02 + ASI05 |
| `tool_descriptions` | Map of tool name to description text (for ASI02) |
| `loaded_components` | List of `{name, signed}` dicts (for ASI04) |
| `capability_violation` / `capability_violation_reason` | ASI03 delegation |
| `code_safe_tools` | Whitelist for ASI05 (lint / format tools) |
| `audit_log_present` | ASI09 - whether the call landed in the chain |
| `stated_intent` / `planned_action` | ASI10 - text comparison |

Detectors that don't see their keys return `INFO` (passed). The
heuristic surface is wide on purpose so a caller that only populates
two keys still gets eight passing detectors out of ten.

---

## Honesty caveats

The detectors are heuristics, not proofs. Two well-known classes of
false positive:

- **ASI01 trips on documentation about prompt injection.** This page
  itself contains the phrase "ignore previous instructions" in its
  example list; the detector would fire on a copy of this docstring
  passed through `prompt`. Reviewers analysing security writeups
  should disable the detector or downgrade severity for the
  in-flight scan.
- **ASI10 is a soft signal.** Misalignment-drift detection compares
  raw verb stems (`read` vs `write`/`delete`/`send`) without semantic
  similarity. A noisy chain-of-thought that mentions both verbs will
  pass even when the action contradicts the intent. Treat ASI10
  findings as a prompt for human review, not as a hard block. The
  semantic-similarity backend that would tighten this is deferred.

The pack is intended to **complement** the deeper modules already
shipped in `core.security` (capability matrix, sandbox-escape detector,
permission graph, audit chain) - it is not a replacement for them.

---

## Plugging in a custom detector

`run_owasp_asi_checks` accepts an alternate detector tuple, and
`OwaspAsiGuardrail` exposes the same hook:

```python
from bernstein.core.security.owasp_asi_detectors import (
    OwaspAsiGuardrail,
    DEFAULT_DETECTORS,
)

custom = OwaspAsiGuardrail(
    detectors=(*DEFAULT_DETECTORS, my_extra_detector),
)
```

A custom detector is any callable matching the `Detector` type:
`Callable[[dict[str, Any]], ASIFinding]`. Crashes inside a detector
are caught and converted to a `CRITICAL` finding so the orchestrator
keeps running with one bad detector temporarily out.

---

## Related

- Source: `src/bernstein/core/security/owasp_asi_detectors.py`
- Pipeline integration: `src/bernstein/core/security/guardrail_pipeline.py`
- [Lethal-trifecta security model](lethal-trifecta.md) - the structural
  capability gate that runs before any guardrail check
- [Capability matrix](capability-matrix.md) - the tool-tag registry
  ASI03 delegates to
- [MCP server signing + supply-chain scan](mcp-signing.md) - the
  deeper signature gate ASI04 delegates to
- OWASP Top 10 for Agentic Apps (December 2025) - upstream framework
  the detector pack tracks
