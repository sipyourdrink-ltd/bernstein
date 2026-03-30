# D11 — Error Code Registry with Documentation Links

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
When errors occur, users see raw exception messages with no guidance on how to resolve them. There is no systematic way to look up error causes or fixes, leading to support requests and frustration.

## Solution
- Create an error code registry at `src/bernstein/errors/registry.py`
- Define error code ranges: E1000-E1999 for `ChatError` (agent communication), E2000-E2999 for `OrchestratorError` (workflow/task management), E3000-E3999 for `AdapterError` (tool/integration issues)
- Each registry entry is a dict with: `code`, `title`, `description`, `suggestion` (fix hint)
- Create a `registry: dict[str, ErrorEntry]` mapping error codes to their metadata
- Modify all existing error classes to accept and store an error code
- Update error message formatting to append: `See: docs.bernstein.dev/errors/E{code}`
- Add a `bernstein error <code>` CLI command that prints the full error description and suggested fix locally (no network required)

## Acceptance
- [ ] `src/bernstein/errors/registry.py` exists with error code ranges for ChatError, OrchestratorError, and AdapterError
- [ ] Every error class (`ChatError`, `OrchestratorError`, `AdapterError`) includes an error code field
- [ ] Raised errors include a `See: docs.bernstein.dev/errors/E{code}` line in their output
- [ ] `bernstein error E1001` prints the error title, description, and suggested fix
- [ ] Running `bernstein error` with an unknown code prints "Unknown error code"
- [ ] At least 5 error codes are populated per error class (15 total minimum)
