# Task server API

Base URL: **http://127.0.0.1:8052**

## Create a task

```bash
curl -s -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Implement feature X",
    "role": "backend",
    "description": "Detailed description with acceptance criteria",
    "priority": 2,
    "scope": "medium",
    "complexity": "medium",
    "owned_files": ["src/path/to/file.py"],
    "completion_signals": [
      {"type": "path_exists", "value": "src/path/to/file.py"},
      {"type": "test_passes", "value": "uv run pytest tests/unit/test_file.py -x -q"}
    ]
  }'
```

## Completion-signal types

- `path_exists` — file / directory must exist.
- `test_passes` — shell command must exit 0.
- `file_contains` — file must contain the string. Format: `path :: needle`.
- `glob_exists` — at least one file matching the glob must exist.

## Other endpoints

- `GET  /tasks?status=open` — list by status.
- `POST /tasks/{id}/complete` — mark done.
- `POST /tasks/{id}/fail` — mark failed.
- `POST /tasks/{id}/progress` — report progress (files_changed,
  tests_passing, errors).
- `POST /bulletin` — cross-agent finding / blocker.
- `GET  /bulletin?since={ts}` — recent bulletins.
