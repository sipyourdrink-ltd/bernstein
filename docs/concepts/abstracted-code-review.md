# Abstracted code review

PRs opened by Bernstein include an **Intent** section: 1-3 bullets per
file describing what changed and why, plus a pseudocode block for
non-trivial functions, with the raw diff folded under
`<details>` for drill-down. Humans reviewing AI-generated diffs get
intent-level context first, line-by-line context only when they need
it.

## Why it exists

Once `bernstein run` starts shipping multiple PRs per hour, the
bottleneck shifts from the agent's wall-clock speed to the human
reviewer's reading speed. The data Bernstein already has - the
spawning task description, agent progress reports, the diff, the test
results - is enough to synthesise a 3-line summary per file. The
abstraction layer turns that data into a PR body humans actually
read.

## How to use it

It is on by default. PRs opened via `bernstein` carry the Intent
section automatically. Disable per run:

```bash
# bernstein.yaml
review:
  abstract_diff: false
```

Or per repo:

```python
# defaults override
ABSTRACT_DIFF_ENABLED = False
```

Programmatic API for custom tooling:

```python
from bernstein.core.quality.review_pipeline.abstract_diff import (
    summarize_diff,
    pseudo_for_function,
)

# summarize_diff is async and returns one IntentSummary per file
summaries = await summarize_diff(diff_text, task_context)
for summary in summaries:
    print(summary.path)
    print(summary.bullet_points)
    print(summary.pseudocode_blocks)
    print(summary.raw_diff_link)
```

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
