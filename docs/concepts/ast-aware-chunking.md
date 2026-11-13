# AST-aware chunking for the reviewer

The reviewer role frequently inspects files larger than its read budget.
Line-based windowing cuts in the middle of functions, drops imports, and
hands the model partial context. **AST-aware chunking** parses the file
with the standard-library `ast` module and splits at top-level statement
boundaries (functions, classes, statement runs), so every chunk the
reviewer sees is a complete syntactic unit.

## Why it exists

Reviewer false-negatives cost real bugs. Other roles read code they
wrote; reviewer reads unfamiliar diffs. The chunker groups top-level
Python units (functions, classes, and statement runs) toward a token
budget. It does not split an oversized function or class into smaller
blocks or lines, so a chunk can exceed the budget.

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
(`python` for syntax-aware chunks; `text` for line-based fallback
chunks). No shipped reviewer prompt
consumes the chunks yet; callers render them.

## Configuration

The chunker takes its budget from the `budget_tokens` argument
(default `4000`; the line-based fallback uses the same value) and is
otherwise self-contained - no user-facing knobs. The budget is a soft
target based on a rough character-count estimate (about four characters
per token), not a hard limit. Python units are packed together up to the
target; a function or class that alone exceeds it is emitted whole, so
that chunk can be over budget. The effective minimum is 64 tokens:
smaller values, including zero or negative values, are clamped to 64.

For example, suppose `report.py` contains only `build_report`, whose full
body is about 900 estimated tokens (the body is abbreviated here). With a
400-token budget the chunker returns the complete function as one chunk,
not several pieces of its body:

```python
# report.py

def build_report():
    # ... long body ...
    return report

chunks = chunk_for_review("report.py", budget_tokens=400)
# chunks[0] contains the complete build_report; its estimate can exceed 400.
```

Non-Python files and Python files that fail to parse use line-based
windows instead of AST units; invalid Python is not returned as one
whole unparsed chunk. Missing, unreadable, or empty files produce an
empty list (`[]`).

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
