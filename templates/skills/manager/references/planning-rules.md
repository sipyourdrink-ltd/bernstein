# Planning rules

1. **Never assign two tasks to the same files** — prevents merge conflicts.
2. **Break large tasks into small ones** (30-60 min each, max 120 min).
3. **Include tests** in every implementation task or as separate QA tasks.
4. **Every task has completion signals** so the janitor can verify.
5. **Check `.sdd/backlog/open/`** for existing starter tickets —
   incorporate them.
6. Dependencies: note them in the description; the system handles ordering.
7. **Include context hints** — for each task, list the specific files,
   functions, and architectural decisions the assigned agent needs to
   know. This eliminates agent orientation time.

Example context hint:

> You'll modify `TaskContextBuilder.build_context()` in
> `src/bernstein/core/context.py`. It uses AST parsing via
> `_parse_python_file()`. Related: `spawner.py` calls it during
> prompt rendering.
