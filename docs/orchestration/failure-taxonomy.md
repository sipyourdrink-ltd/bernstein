# Failure taxonomy in tracker comments

When an agent run fails on a tracker ticket, Bernstein posts a comment
back to the originating tracker (GitHub Projects v2, Jira, Linear,
GitLab, etc.). Historically the comment was free text. Downstream
automation (auto-escalation, retry-with-continuation, dead-letter
pipelines) could not parse it reliably.

This page documents the structured failure summary that now ships
inside every failure comment.

## TL;DR

Every failure comment posted by Bernstein contains:

1. A human-readable preamble (one short paragraph).
2. A fenced YAML block tagged ``bernstein-failure-v1``.
3. An optional triple-backticked traceback block.

Downstream consumers look for the fenced block and parse the YAML with
any conformant YAML 1.1 / 1.2 reader.

## YAML schema

```bernstein-failure-v1
reason_code: timeout
category: timeout
transient: true
next_action: retry
evidence_path: logs/run.log
```

| Field           | Type      | Description |
|-----------------|-----------|-------------|
| `reason_code`   | string    | Closed-set machine code (see below). |
| `category`      | string    | Broader bucket from `FailureCategory`. |
| `transient`     | bool      | True when retry is likely to recover. |
| `next_action`   | string    | Short imperative hint (`retry`, `escalate`, `page_oncall`, ...). |
| `evidence_path` | string    | Relative path to a log or trace file, or `""`. |

The fenced info-string is versioned. A future schema bump moves to
`bernstein-failure-v2`. Downstream parsers MUST treat unknown info-
strings as opaque and skip them; they MUST NOT attempt to coerce.

## Closed-set reason codes

| Code                  | Typical cause |
|-----------------------|---------------|
| `test_regression`     | Existing tests broken by the agent's diff. |
| `timeout`             | Run hit a wall-clock or turn limit. |
| `rate_limit`          | Provider returned 429 or equivalent. |
| `network_error`       | DNS, connection reset, refused, etc. |
| `sandbox_violation`   | Agent attempted a denied operation. |
| `missing_dependency`  | `ModuleNotFoundError` or unresolved import. |
| `type_error`          | `TypeError` / `AttributeError` at runtime. |
| `syntax_error`        | `SyntaxError` / `IndentationError`. |
| `flaky_test`          | Test passes on retry; non-deterministic. |
| `scope_violation`     | Agent modified files outside `owned_files`. |
| `merge_conflict`      | Conflict markers or rebase conflict detected. |
| `compile_error`       | Compiler rejected the diff. |
| `context_miss`        | Agent lacked context to complete the task. |
| `unknown`             | Heuristics did not identify a category. |

Consumers MUST treat unrecognised codes as `unknown` and fall back to
the default escalation policy. Adding a new code is a single-line
change to `FAILURE_REASON_CODES` in
`src/bernstein/core/orchestration/failure_taxonomy.py`; the AGENTS.md
mirrors do not need to change.

## Consumer contract

A downstream parser:

1. Searches the comment body for the literal fence
   `` ```bernstein-failure-v1\n ``.
2. Reads until the next ``\n``` `` marker.
3. Passes the inner text to a YAML 1.1+ `safe_load`.
4. Returns a successfully parsed mapping without checking the consumer's
   required fields or their types.
5. Leaves schema validation and fallback to the consumer.

The reference parser is `parse_failure_comment` in
`bernstein.core.orchestration.failure_taxonomy`. It normalizes LF, CRLF,
and bare-CR line endings to LF before searching. The opening fence must be
the literal tagged fence followed by a newline. If the opening fence or a
subsequent closing marker is absent, it returns an empty result (`raw ==
""`, `payload == {}`). A present but empty YAML block also yields those
values because it parses to a non-mapping, so the result fields alone do
not distinguish it from a missing block. If a non-empty block is found but
its YAML is malformed or parses to a non-mapping, it returns that block in
`raw` and an empty `payload`.

A parsed mapping can be partial. For example, a block containing only
`reason_code: timeout` yields `{"reason_code": "timeout"}`; extraction
does not enforce the fields in the documented schema. Before trusting
any fields, a consumer must validate that the result is a mapping with
`reason_code`, `category`, `transient`, `next_action`, and
`evidence_path`, and check their types and allowed values. If extraction
or schema validation fails—including when required fields are missing—
reject the structured result and fall back to the legacy free-text path;
do not infer defaults for missing fields. After schema validation, an
unrecognised reason code maps to `unknown` and uses the default escalation
policy described above.

## Worked example

```python
from bernstein.core.orchestration.failure_taxonomy import (
    render_failure_comment,
)

try:
    run_agent(...)
except Exception as exc:
    body, classification = render_failure_comment(
        exc,
        context={"summary": "pytest tests/unit/ failed at orientation phase"},
        evidence_path=".sdd/traces/run-2026-05-19.jsonl",
    )
    tracker.add_comment(ticket_id, body)
    emit_lifecycle_event(
        "tracker.failure_taxonomy",
        reason_code=classification.reason_code,
        category=classification.category.value,
        transient=classification.transient,
    )
```

The tracker comment posted by the snippet above is parseable by every
downstream pipeline that follows the consumer contract above.

## Lifecycle event

The same payload is emitted as a `tracker.failure_taxonomy` lifecycle
event so subscribers that do not poll trackers (in-process listeners,
metrics exporters) can react without parsing comments.

Auto-escalation rules that act on the lifecycle event ship in a
separate operator-config ticket; this module is the structural
prerequisite.
