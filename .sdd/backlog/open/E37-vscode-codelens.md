# E37 — VS Code CodeLens Extension

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Developers must manually type CLI commands to run Bernstein against specific test files, adding friction to the fix-test workflow.

## Solution
- Extend the VS Code extension with a CodeLens provider for test files.
- Detect test files by pattern (`*_test.py`, `test_*.py`, `*.test.ts`, `*.spec.ts`, etc.).
- Show "Run with Bernstein" CodeLens above test functions and test file headers.
- Clicking the CodeLens executes `bernstein run -g "fix failing test in <file>:<function>"` in the integrated terminal.
- Support configuration for custom test file patterns via extension settings.

## Acceptance
- [ ] CodeLens appears above test functions in supported test files
- [ ] Clicking CodeLens runs the correct bernstein command with file and function context
- [ ] Custom test file patterns can be configured in extension settings
- [ ] CodeLens does not appear in non-test files
