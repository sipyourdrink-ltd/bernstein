# Bernstein GitHub Action

Run [Bernstein](https://github.com/chernistry/bernstein) from GitHub Actions to orchestrate CLI coding agents on your repository.

```yaml
- uses: chernistry/bernstein-action@v1
  with:
    task: "Fix all lint errors and ensure tests pass"
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `task` | yes | — | Task description, or `fix-ci` to auto-fix failing CI |
| `budget` | no | `5.00` | Dollar cap for the run (0 = unlimited) |
| `cli` | no | `claude` | Agent CLI to use: `claude`, `codex`, `gemini`, `qwen` |
| `max-retries` | no | `3` | Retry attempts in `fix-ci` mode |
| `python-version` | no | `3.12` | Python version to install |
| `post-comment` | no | `true` | Post a PR/issue comment with the orchestration summary |

## Outputs

| Output | Description |
|--------|-------------|
| `tasks-completed` | Number of tasks completed |
| `total-cost` | Total API cost in USD |
| `pr-url` | Pull request URL created by Bernstein (if any) |
| `evidence-bundle-path` | Path to evidence bundle (logs, test results, cost report) |

---

## Trigger modes

### 1. On pull request — review and test

Automatically review every non-draft PR. Bernstein runs your test suite, checks code quality, and posts a comment with findings.

```yaml
# .github/workflows/bernstein-pr-review.yml
name: "Bernstein PR Review"
on:
  pull_request:
    types: [opened, synchronize, ready_for_review]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    if: >
      !github.event.pull_request.draft &&
      !contains(github.event.pull_request.labels.*.name, 'skip-bernstein')
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0

      - uses: chernistry/bernstein-action@v1
        with:
          task: >
            Review PR #${{ github.event.pull_request.number }}:
            "${{ github.event.pull_request.title }}".
            Run tests, check types, identify issues. Post findings as a comment.
            Do not push changes.
          budget: "3.00"
          post-comment: "true"
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

---

### 2. On CI failure — auto-fix

Trigger Bernstein when a CI workflow fails. It downloads the failed logs, identifies the root cause, and pushes a fix.

```yaml
# .github/workflows/bernstein-ci-fix.yml
name: "Bernstein CI Fix"
on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]
    branches: [main]

concurrency:
  group: bernstein-fix-${{ github.event.workflow_run.head_branch }}
  cancel-in-progress: true

permissions:
  contents: write
  pull-requests: write
  actions: read

jobs:
  fix:
    runs-on: ubuntu-latest
    if: ${{ github.event.workflow_run.conclusion == 'failure' }}
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.workflow_run.head_branch }}
          fetch-depth: 0

      - uses: chernistry/bernstein-action@v1
        with:
          task: fix-ci
          budget: "5.00"
          max-retries: "3"
          post-comment: "true"
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

---

### 3. On issue label — decompose and implement

Label any issue with `bernstein` and Bernstein will implement it automatically and open a PR.

```yaml
# .github/workflows/bernstein-issues-decompose.yml
name: "Bernstein Issue Decompose"
on:
  issues:
    types: [labeled]

permissions:
  contents: write
  issues: write
  pull-requests: write

jobs:
  decompose:
    runs-on: ubuntu-latest
    if: github.event.label.name == 'bernstein'
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: chernistry/bernstein-action@v1
        with:
          task: >
            Implement issue #${{ github.event.issue.number }}:
            "${{ github.event.issue.title }}".
            ${{ github.event.issue.body }}
            Run tests, then open a PR referencing this issue.
          budget: "10.00"
          post-comment: "true"
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

---

### 4. On schedule — periodic maintenance

Run weekly codebase maintenance: dependency updates, lint fixes, dead code removal.

```yaml
# .github/workflows/bernstein-scheduled-maintenance.yml
name: "Bernstein Scheduled Maintenance"
on:
  schedule:
    - cron: "0 3 * * 1"  # Every Monday at 03:00 UTC
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

jobs:
  maintenance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: chernistry/bernstein-action@v1
        with:
          task: >
            Perform weekly codebase maintenance:
            1. Update outdated dependencies (minor/patch only).
            2. Fix any new ruff lint warnings.
            3. Remove unused imports and dead code.
            4. Ensure tests still pass.
            5. Open a PR titled "[maintenance] Weekly automated cleanup".
          budget: "8.00"
          post-comment: "false"
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

---

## Configuration

Place a `bernstein.yaml` in your repo root to customize behaviour. If none exists, Bernstein creates a minimal one automatically.

```yaml
# bernstein.yaml
cli: claude          # or codex, gemini, qwen
max_agents: 4
constraints:
  - "Run tests before marking tasks complete"
  - "Commit after each task with a descriptive message"
```

## Publishing to GitHub Marketplace

See [`.sdd/backlog/manual/M007-github-action-marketplace.md`](../.sdd/backlog/manual/M007-github-action-marketplace.md) for the step-by-step publish checklist. This is a one-time manual step — GitHub requires clicking "Publish" in the UI after tagging a release.

## API keys

Bernstein delegates work to whichever CLI agent you configure. Add the corresponding secret to your repository:

| CLI | Secret |
|-----|--------|
| Claude Code (`claude`) | `ANTHROPIC_API_KEY` |
| Codex (`codex`) | `OPENAI_API_KEY` |
| Gemini CLI (`gemini`) | `GOOGLE_API_KEY` |
| Qwen (`qwen`) | `DASHSCOPE_API_KEY` |
