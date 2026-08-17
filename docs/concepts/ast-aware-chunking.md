# AST-aware chunking for the reviewer

The reviewer role frequently inspects files larger than its read budget.
Line-based windowing cuts in the middle of functions, drops imports, and
hands the model partial context. **AST-aware chunking** uses the existing
Python symbol graph to split files at function and class boundaries, so
every chunk the reviewer sees is a complete syntactic unit.

## Why it exists

Reviewer false-negatives cost real bugs. Other roles read code they
wrote; reviewer reads unfamiliar diffs. Splitting on the largest
semantic unit that fits the budget (function → class → block → line)
gives the reviewer denser context per token and prevents the
"I-saw-half-a-function" failure mode.

## How to use it

The chunker is invoked automatically by the review pipeline whenever
the reviewer would otherwise window-read a Python file larger than its
budget. There is no flag to set per-run - it is on by default.

If you are calling the chunker directly from custom tooling:

```python
from bernstein.core.quality.review_pipeline.ast_chunker import (
    chunk_for_review,
)

chunks = chunk_for_review(
    path="src/bernstein/core/orchestration/manager.py",
    budget_tokens=4_000,
)
for chunk in chunks:
    print(chunk.header)  # symbols included in this chunk
    print(chunk.text)  # full Python source, never split mid-body
```

Each `ReviewChunk` carries the symbol header (function or class names
included), the byte range, and the full source text. The reviewer
prompt assembles them with the header as a one-line summary so the
model sees structure before code.

## Configuration

The chunker reads `defaults.REVIEW_BUDGET_TOKENS` (the same budget the
line-based fallback uses) and is otherwise self-contained - no
user-facing knobs.

## Limitations

- Python only. TypeScript / Rust / Go fall back to line-based
  windowing with a clear log line so you can see what's degraded.
- The chunker does not synthesise summaries - it only segments. The
  review prompt is unchanged.
- Cross-file dependencies are not packaged into a chunk. If reviewing
  function `foo` requires reading `bar` from a different file, the
  reviewer asks for `bar` on a follow-up turn the same way it does
  today.

## Related

- Source: `src/bernstein/core/quality/review_pipeline/ast_chunker.py`
- Symbol graph: `src/bernstein/core/knowledge/ast_symbol_graph.py`
- Quality pipeline: [Quality Pipeline](../architecture/quality-pipeline.md)
- PR #993
