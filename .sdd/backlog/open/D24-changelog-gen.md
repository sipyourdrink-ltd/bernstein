# D24 — Changelog Generation from Completed Tasks

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
After multiple Bernstein runs, there's no way to see a summary of all changes made over time. Users lose track of what was done and when, making it hard to communicate progress to teammates.

## Solution
- Implement `bernstein changelog` that generates a changelog from completed task descriptions in `.sdd/backlog/closed/`.
- Read each closed ticket's title and completion date.
- Group entries by date, most recent first.
- Format output as:
  ```
  ## 2025-03-15
  - Add authentication middleware: Implemented JWT-based auth for all API routes
  - Fix database connection pooling: Resolved connection leak under high concurrency

  ## 2025-03-14
  - Generate API documentation: Created OpenAPI spec from route definitions
  ```
- Default output goes to stdout for piping.
- Support `--output CHANGELOG.md` to write directly to a file.
- Support `--since <date>` to filter entries after a specific date.
- Support `--format <md|json>` for output format selection.

## Acceptance
- [ ] `bernstein changelog` outputs a formatted changelog grouped by date from closed tasks
- [ ] Entries are sorted with most recent dates first
- [ ] `--output CHANGELOG.md` writes the changelog to the specified file
- [ ] `--since 2025-03-14` filters to only show entries from that date onward
- [ ] `--format json` outputs valid JSON with the same grouped structure
