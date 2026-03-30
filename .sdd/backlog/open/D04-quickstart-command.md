# D04 — `bernstein quickstart` Zero-Config Demo

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
New users want to see Bernstein in action before investing time in configuration. There is no way to try it without first setting up a project and writing a workflow file.

## Solution
- Add a `bernstein quickstart` command
- Clone or copy the bundled `examples/quickstart` demo project into a temporary directory (`tempfile.mkdtemp()`)
- The demo project should contain a pre-configured `bernstein.yaml` with a simple 3-task workflow (e.g., generate a function, write tests, run linter)
- Execute the workflow automatically and stream output to the terminal
- At completion, print the results summary and the path to the temp directory so users can inspect the output
- Ensure the command works with zero prior configuration (no existing `bernstein.yaml` required)
- Add a `--keep` flag to preserve the temp directory; default behavior cleans it up

## Acceptance
- [ ] Running `bernstein quickstart` with no prior setup completes successfully
- [ ] The command creates a temp directory, runs a 3-task workflow, and prints results
- [ ] Output clearly shows each task starting, progressing, and completing
- [ ] The temp directory path is printed so users can inspect generated artifacts
- [ ] `bernstein quickstart --keep` preserves the temp directory after completion
- [ ] The command does not require or modify any existing `bernstein.yaml` in the user's project
