# Abstracted code review

Bernstein's standard PR entry points do **not** currently add an **Intent**
section. The library provides an opt-in API for custom tooling to create
1-3 bullets per file describing what changed and why, plus pseudocode for
non-trivial functions. When rendered with raw diff input, the section
provides drill-down to the diff. This gives reviewers intent-level context
first and line-by-line context when they need it.

## Why it exists

Once `bernstein run` starts shipping multiple PRs per hour, the
bottleneck shifts from the agent's wall-clock speed to the human
reviewer's reading speed. The data Bernstein already has - the
spawning task description, agent progress reports, the diff, the test
results - is enough to synthesise a 3-line summary per file. The
abstraction layer turns that data into a PR body humans actually
read.

## How to use it

The abstraction is enabled by default at the library level
(`ABSTRACT_DIFF_ENABLED = True`), but no shipped PR path calls it yet:
`bernstein pr` and the orchestrator's PR body do not include the Intent
section. Tooling that wants it calls
`bernstein.core.review_responder.pr_gen.build_pr_body` (or
`summarize_diff` + `render_pr_body`) directly. The switch is the module
constant `bernstein.core.defaults.ABSTRACT_DIFF_ENABLED`; there is no
`bernstein.yaml` key for it. `abstract_diff` copies the value at import,
so reassigning `defaults.ABSTRACT_DIFF_ENABLED` after that module is
loaded has no effect. To disable per call, pass `enabled=False` to
`summarize_diff` (or `build_pr_body`):

```python
summaries = await summarize_diff(diff, task_context, enabled=False)
```

Custom tooling can use the asynchronous `summarize_diff` API to obtain
`IntentSummary` values, `render_pr_body` to format those values, or
`build_pr_body` to run both steps and append the result to a description.
For example, the callback below makes the example runnable without an LLM
or network access:

```python
import asyncio
import json

from bernstein.core.quality.review_pipeline.abstract_diff import (
    TaskContext,
    render_pr_body,
    summarize_diff,
)
from bernstein.core.review_responder.pr_gen import build_pr_body


diff = """
diff --git a/src/retry.py b/src/retry.py
--- a/src/retry.py
+++ b/src/retry.py
@@ -1 +1 @@
-retry()
+retry_with_backoff()
"""
task_context = TaskContext(title="Fix retry loop")


async def stub_llm(*, prompt: str, model: str, **kwargs: object) -> str:
    return json.dumps({"bullets": ["Retries failed operations with backoff"], "confidence": 0.9})


async def main() -> None:
    summaries = await summarize_diff(diff, task_context, llm_caller=stub_llm)
    intent_section = render_pr_body(summaries, raw_diff=diff)
    body = await build_pr_body(
        description="Fixes retries.",
        diff=diff,
        task_context=task_context,
        llm_caller=stub_llm,
    )
    assert "Retries failed operations with backoff" in intent_section
    assert "## Intent" in body


asyncio.run(main())
```

`stub_llm` stands in for the async LLM callback; real callers may omit it
to use the default cheap-tier caller. `summarize_diff` returns one summary
per changed file. If disabled (`enabled=False`) or given an empty diff, it
returns `[]`; `render_pr_body([])` returns an empty string, and
`build_pr_body` then returns the description text (with trailing whitespace
stripped) without an Intent section. These empty results do not require a
callback.

Above the file cap, `summarize_diff` makes no per-file callback calls and
returns one summary at path `<aggregate>` with an aggregate bullet instead
of per-file summaries. `render_pr_body` renders that summary. A supplied
`raw_diff_link` is rendered as a link; the raw diff text does not match the
aggregate path, and the renderer only adds its full-diff block when there
are multiple summaries. For ordinary per-file summaries, passing `raw_diff`
to `render_pr_body` (as `build_pr_body` does) puts each matching file's
diff in a collapsible block and, when there is more than one summary, adds
a full-diff block as well. Without raw diff text, each summary can instead
render its `raw_diff_link`.

The summariser uses the cheap-tier model via the cascade router; cost
is tracked alongside any other LLM call.

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `defaults.ABSTRACT_DIFF_ENABLED` | `true` | Master switch. |
| `defaults.ABSTRACT_DIFF_MAX_FILES` | `50` | Above this, the per-file abstraction degrades to a top-level summary only. |
| Model tier | cheap-tier (cascade router) | Opus is disallowed at this layer. |

## Limitations

- The summary is LLM-generated. It is not a formal proof that the
  pseudocode matches the real code - confidence scoring + drill-down
  are the safety net.
- Control-flow / data-flow diagrams are out of scope for the summariser.
- This augments the PR body. It does **not** replace the rubric-based
  review verdict - the existing reviewer gate still runs.
- Diffs > 50 files fall back to a top-level summary, not per-file
  abstractions, to keep the PR body readable.

## Related

- Source: `src/bernstein/core/quality/review_pipeline/abstract_diff.py`
- PR generation: `src/bernstein/core/review_responder/pr_gen.py`
- [Quality Pipeline](../architecture/quality-pipeline.md)
- PR #1005
