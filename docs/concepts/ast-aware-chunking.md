# AST-aware chunking for the reviewer

The reviewer role frequently inspects files larger than its read budget.
Line-based windowing cuts in the middle of functions, drops imports, and
hands the model partial context. **AST-aware chunking** parses the file
with the standard-library `ast` module and splits at top-level statement
boundaries (functions, classes, statement runs), so every chunk the
reviewer sees is a complete syntactic unit.

## Why it exists

Reviewer false-negatives cost real bugs. Other roles read code they
wrote; reviewer reads unfamiliar diffs. Splitting on the largest
semantic unit that fits the budget (function → class → block → line)
gives the reviewer denser context per token and prevents the
"I-saw-half-a-function" failure mode.

## How to use it

The chunker is exported from the review pipeline package and is
called directly by tooling that needs to feed a Python file to the
reviewer within a token budget. There is no flag to set per-run.

To call the chunker from custom tooling:

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

Each `ReviewChunk` carries `path`, `start_line` / `end_line` (1-indexed,
inclusive), the top-level `symbols`, the source `text`, a one-line
`header` (`# <path> L<start>-<end> - symbols: ...`) and `language`
(`python` or `text` for the fallback). No shipped reviewer prompt
consumes the chunks yet; callers render them.

## Configuration

The chunker takes its budget from the `budget_tokens` argument
(default `4000`; the line-based fallback uses the same value) and is
otherwise self-contained - no user-facing knobs.

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
- Quality pipeline: [Quality Pipeline](../architecture/quality-pipeline.md)
- PR #993
