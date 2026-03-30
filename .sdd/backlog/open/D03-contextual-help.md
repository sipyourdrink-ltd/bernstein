# D03 — Contextual Help with Real-World Examples

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
The `--help` text for CLI subcommands only shows parameter descriptions. Users cannot see practical usage patterns without leaving the terminal to read external docs.

## Solution
- Add real-world usage examples to every CLI subcommand using Click's `epilog` parameter
- Each epilog should contain 2-4 concrete command-line examples with common flag combinations
- Format examples consistently: a short description line followed by the command prefixed with `$`
- Cover the most common use cases for each command (e.g., `bernstein run --agent claude --parallel 4`, `bernstein status --watch`, `bernstein logs --task 3 --tail`)
- Use `click.style()` or Rich markup to highlight example commands vs. descriptions
- Ensure epilog text is wrapped correctly for standard 80-column terminals

## Acceptance
- [ ] Every CLI subcommand's `--help` output includes an "Examples" section
- [ ] Each examples section contains at least 2 real-world command invocations
- [ ] Examples use actual flags and arguments that the command supports
- [ ] Help text renders correctly in a standard 80-column terminal without line-wrapping artifacts
- [ ] Running `bernstein run --help` shows examples for common run scenarios
