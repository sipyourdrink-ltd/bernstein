# D08 — `bernstein explain` Human-Readable Task Narrative

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
Task traces are stored as raw structured data (JSON/logs) that require manual inspection to understand what an agent did. Users want a quick plain-English summary without digging through log files.

## Solution
- Add a `bernstein explain <task-id>` command
- Read the task trace data from the `.sdd/traces/` directory for the given task ID
- Parse the trace to extract key events: files read, files written, commands run, test results, cost, duration, agent used
- Generate a human-readable narrative paragraph, e.g.: "Agent claude-3-opus was assigned task #4 'Add JWT middleware'. It read 3 files, wrote 2 files, ran tests (all passed), cost $0.08, took 34 seconds."
- Include a breakdown section listing specific files read and written
- If tests were run, include pass/fail counts
- Support `--json` flag to output the structured summary as JSON instead of narrative text
- Support `--verbose` flag to include the full sequence of agent actions in chronological order

## Acceptance
- [ ] `bernstein explain <task-id>` prints a readable narrative summary of the task
- [ ] The narrative includes: agent name, task title, files read count, files written count, test results, cost, and duration
- [ ] `bernstein explain <task-id> --json` outputs structured JSON
- [ ] `bernstein explain <task-id> --verbose` lists each agent action in order
- [ ] Running with an invalid task ID prints a clear error message
- [ ] The command works for both completed and failed tasks
