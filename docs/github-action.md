# GitHub Action

Run Bernstein from GitHub Actions to orchestrate coding agents in CI.

## Quick setup

Copy `.github/workflows/bernstein-ci-fix.yml` into your repo, set the
appropriate API key secret, and you're done. When CI fails on your default
branch, Bernstein will attempt to fix it automatically.

## Inputs

| Input            | Required | Default   | Description                                             |
|------------------|----------|-----------|---------------------------------------------------------|
| `task`           | no       | —         | Task description, or `"fix-ci"` for auto-fix mode       |
| `plan`           | no       | —         | Path to a YAML plan file (e.g. `plans/api.yaml`)        |
| `budget`         | no       | `"5.00"`  | Dollar cap for the run                                  |
| `cli`            | no       | `"claude"`| Agent CLI to use (`claude`, `codex`, `gemini`, `qwen`)  |
| `max-retries`    | no       | `"3"`     | Retry count in fix-ci mode                              |
| `python-version` | no       | `"3.12"`  | Python version to install                               |

## Modes

### Plan mode (`plan: path/to/plan.yaml`)

When `plan` is provided, the action runs the specified YAML project plan:

```
bernstein run <plan> --budget <budget> --headless
```

Use this for complex multi-stage migrations, refactorings, or new feature
build-outs described in a YAML plan file.

### Fix-CI mode (`task: fix-ci`)

When `task` is the literal string `"fix-ci"`, the action:

1. Downloads failed job logs from the triggering workflow run (via `gh run view --log-failed`).
2. Passes the logs as context to `bernstein -g "<goal>" --headless`.
3. Retries up to `max-retries` times if the fix attempt fails.
4. Commits and pushes any resulting changes.

This mode is designed for `workflow_run` triggers so it can react to CI
failures from another workflow.

### Normal mode

When `task` is anything other than `"fix-ci"`, the action runs:

```
bernstein -g "<task>" --budget <budget> --headless
```

Use this for ad-hoc tasks like generating tests, refactoring, or applying
a migration.

**Example — run a task on push:**

```yaml
on:
  push:
    branches: [main]

jobs:
  update-docs:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: chernistry/bernstein-action@v4
        with:
          task: "Update API docs to match current source"
          budget: "2.00"
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

## Required secrets

The action needs an API key for whichever agent CLI you use:

| CLI       | Secret                |
|-----------|-----------------------|
| `claude`  | `ANTHROPIC_API_KEY`   |
| `codex`   | `OPENAI_API_KEY`      |
| `gemini`  | `GOOGLE_API_KEY`      |
| `qwen`    | `DASHSCOPE_API_KEY`   |

Set the secret in your repo settings under **Settings > Secrets and variables > Actions**.

## How it works

The action is a composite action (`action.yml` + `action/entrypoint.sh`). Steps:

1. Install Python and uv.
2. Install bernstein via `uv tool install bernstein`.
3. Create a minimal `bernstein.yaml` if one doesn't exist.
4. Run bernstein in headless mode with the specified task and budget.
5. If any files changed, commit and push them.

## Limitations

- **Agent CLI must be available.** The action installs bernstein but not the
  agent CLIs themselves. Claude Code, Codex CLI, etc. need to be installed
  separately or be available in the runner image. For most cases, the API key
  alone is sufficient since bernstein can invoke agent APIs directly.
- **Budget is advisory.** The budget cap relies on bernstein's cost tracking,
  which depends on the agent CLI reporting costs accurately.
- **Fix-CI mode is best-effort.** Complex failures (infra issues, flaky tests,
  missing credentials) may not be fixable by an agent.
- **Concurrency.** The example workflow uses `concurrency` to prevent multiple
  fix attempts from racing. If you run bernstein in other workflows, consider
  adding similar guards.
- **Permissions.** The action needs `contents: write` to push commits and
  `actions: read` to download workflow logs in fix-ci mode.
