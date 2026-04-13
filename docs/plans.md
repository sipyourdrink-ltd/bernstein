# Bernstein Plan Files

Plan files are YAML documents that describe a multi-stage project as a dependency graph of agent tasks. They let you skip the LLM planning phase and go straight to execution.

```sh
bernstein run my-project.yaml
```

## When to use a plan file

Use a plan file when you know the work upfront and want deterministic, reproducible execution. The orchestrator reads the YAML, creates tasks, wires up the dependency graph, and launches agents — no manager LLM step needed.

Use the natural-language goal (`bernstein --goal "..."`) when the work is exploratory and you want the manager agent to decompose it.

## Minimal example

```yaml
name: "Add user authentication"

stages:
  - name: "Backend"
    steps:
      - title: "Implement JWT auth middleware"
        role: backend

  - name: "Tests"
    depends_on: ["Backend"]
    steps:
      - title: "Add auth integration tests"
        role: qa
```

Run it:

```sh
bernstein run auth.yaml
```

## Full field reference

### Plan-level fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Short plan name — used as the orchestration goal |
| `description` | no | Human-readable summary |
| `constraints` | no | List of constraint strings injected into every agent |
| `context_files` | no | Extra files injected into agent context |
| `cli` | no | Force a CLI agent: `auto`, `claude`, `codex`, `gemini` |
| `budget` | no | Spending cap: `"$10"`, `"5.00"`, etc. |
| `max_agents` | no | Max concurrent agent processes |

### Stage fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Stage identifier (used in `depends_on`) |
| `description` | no | Human-readable stage summary |
| `depends_on` | no | List of stage names that must complete first |
| `steps` | yes | List of step definitions |

### Step fields

| Field | Required | Description |
|-------|----------|-------------|
| `title` | yes* | Short task title (*or use legacy `goal` field) |
| `description` | no | Detailed agent instructions (falls back to `title`) |
| `role` | no | Specialist role: `backend`, `qa`, `frontend`, `security`, `devops`, `docs`, `architect` (default: `backend`) |
| `scope` | no | Duration estimate: `small`, `medium`, `large` (default: `medium`) |
| `complexity` | no | Reasoning difficulty: `low`, `medium`, `high` (default: `medium`) |
| `priority` | no | `1`=critical, `2`=normal, `3`=nice-to-have (default: `2`) |
| `model` | no | Model override: `opus`, `sonnet`, `haiku`, `auto` |
| `effort` | no | Effort level: `low`, `normal`, `high`, `max` |
| `estimated_minutes` | no | Time budget hint (default: `30`) |
| `files` | no | Files the agent will modify (used for conflict detection) |
| `completion_signals` | no | Machine-checkable done criteria (see below) |

## Dependency model

Stages run sequentially. Steps within a stage run in parallel.

`depends_on` at the stage level means every step in this stage depends on every step in the listed stages:

```yaml
stages:
  - name: "Foundation"
    steps:
      - title: "Create database schema"
        role: backend
      - title: "Create S3 bucket"
        role: devops

  - name: "App"
    depends_on: ["Foundation"]   # waits for BOTH Foundation steps
    steps:
      - title: "Implement user service"
        role: backend
```

## Completion signals

Completion signals let the Janitor verify tasks automatically without LLM review:

```yaml
completion_signals:
  - type: path_exists
    path: "src/auth/middleware.py"

  - type: test_passes
    command: "pytest tests/test_auth.py -x -q"

  - type: file_contains
    path: "src/auth/middleware.py"
    contains: "def verify_token"

  - type: glob_exists
    path: "src/auth/*.py"

  - type: llm_review
    value: "JWT middleware is implemented and handles token expiry correctly"
```

Signal types:

| Type | Value field | Passes when |
|------|-------------|-------------|
| `path_exists` | file path | File exists |
| `glob_exists` | glob pattern | At least one match exists |
| `test_passes` | shell command | Command exits 0 |
| `file_contains` | search string | File contains the string |
| `llm_review` | instruction | LLM judges the work as complete |
| `llm_judge` | instruction | Alias for `llm_review` |

## Full example

```yaml
name: "REST API with auth"
description: >
  Build a FastAPI service with JWT authentication, user CRUD, and full test coverage.

constraints:
  - "Python 3.12+"
  - "pytest for tests"
  - "ruff for linting"

context_files:
  - "docs/api-spec.md"

stages:
  - name: "Data layer"
    steps:
      - title: "Create SQLAlchemy models"
        description: >
          Define User and Session models in src/models.py.
          Use UUID primary keys. Add created_at/updated_at timestamps.
        role: backend
        scope: small
        complexity: low
        files:
          - "src/models.py"
        completion_signals:
          - type: path_exists
            path: "src/models.py"
          - type: file_contains
            path: "src/models.py"
            contains: "class User"

  - name: "Auth"
    depends_on: ["Data layer"]
    steps:
      - title: "Implement JWT middleware"
        description: >
          Create src/auth.py with token creation, verification, and a
          FastAPI dependency that extracts the current user from the
          Authorization header.
        role: backend
        scope: medium
        complexity: medium
        model: sonnet
        files:
          - "src/auth.py"
        completion_signals:
          - type: path_exists
            path: "src/auth.py"
          - type: file_contains
            path: "src/auth.py"
            contains: "def verify_token"

      - title: "Add auth routes"
        description: "POST /auth/login and POST /auth/refresh endpoints."
        role: backend
        scope: small
        files:
          - "src/routes/auth.py"

  - name: "QA"
    depends_on: ["Auth"]
    steps:
      - title: "Write auth integration tests"
        role: qa
        scope: medium
        estimated_minutes: 45
        completion_signals:
          - type: test_passes
            command: "pytest tests/test_auth.py -x -q"
```

## Python API

The plan loader is available as a Python module:

```python
from pathlib import Path
from bernstein.core.planning.plan_loader import load_plan, load_plan_from_yaml, PlanLoadError

# Full API — returns (PlanConfig, list[Task])
config, tasks = load_plan(Path("my-project.yaml"))
print(config.name)          # "REST API with auth"
print(config.constraints)   # ["Python 3.12+", ...]
print(len(tasks))           # total step count

# Simple API — returns list[Task] only
tasks = load_plan_from_yaml(Path("my-project.yaml"))
```

`PlanConfig` fields mirror the plan-level YAML fields: `name`, `description`, `constraints`, `context_files`, `cli`, `budget`, `max_agents`.

### Error handling

```python
from bernstein.core.planning.plan_loader import PlanLoadError

try:
    config, tasks = load_plan(Path("plan.yaml"))
except PlanLoadError as e:
    print(f"Bad plan: {e}")
```

`PlanLoadError` is raised for:

- File not found
- Invalid YAML syntax
- Missing `stages` list
- Stage missing `name`
- Step missing `title` (or legacy `goal`)
