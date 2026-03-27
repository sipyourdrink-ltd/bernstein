# Progress Reporting

Every 60 seconds, report your progress to the task server so the orchestrator
can detect if you are stuck and intervene before token waste accumulates.

## Command

```bash
curl -s -X POST http://127.0.0.1:8052/tasks/{TASK_ID}/progress \
  -H "Content-Type: application/json" \
  -d '{
    "files_changed": <number of files you have modified>,
    "tests_passing": <number of tests currently passing, -1 if unknown>,
    "errors": <number of active errors or compilation failures>
  }'
```

Replace `{TASK_ID}` with your actual task ID (available in your task context).

## Fields

| Field | Type | Description |
|-------|------|-------------|
| `files_changed` | int | Files modified since you started this task |
| `tests_passing` | int | Tests passing right now (-1 if you have not run tests yet) |
| `errors` | int | Active compilation errors or test failures (0 = clean) |

## Behaviour

The orchestrator reads these snapshots and compares consecutive reports:

- **3 identical reports** (~3 min of no progress): you receive a `WAKEUP` signal in
  `.sdd/runtime/signals/{SESSION_ID}/WAKEUP` — read it and address the concern.
- **5 identical reports** (~5 min): you receive a `SHUTDOWN` signal — save WIP and exit.
- **7 identical reports** (~7 min): the orchestrator kills your process.

Progress is measured by changes to `files_changed`, `tests_passing`, or `errors`.
Simply opening a new file does not count as progress.

## Example workflow

```bash
# After making your first edits
curl -s -X POST http://127.0.0.1:8052/tasks/abc123/progress \
  -H "Content-Type: application/json" \
  -d '{"files_changed": 2, "tests_passing": -1, "errors": 3}'

# After running tests
curl -s -X POST http://127.0.0.1:8052/tasks/abc123/progress \
  -H "Content-Type: application/json" \
  -d '{"files_changed": 4, "tests_passing": 14, "errors": 0}'
```
