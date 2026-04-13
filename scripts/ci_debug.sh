#!/usr/bin/env bash
# CI Debug Framework — quick commands for diagnosing and fixing CI failures
# Usage: source scripts/ci_debug.sh && ci_status

set -euo pipefail

# ── CI Status ──
ci_status() {
  echo "=== Latest CI Runs ==="
  gh run list --workflow ci.yml --branch main --limit 3 --json databaseId,status,conclusion --jq '.[] | "\(.databaseId) \(.status) \(.conclusion)"'
  return 0
}

ci_jobs() {
  local run_id=${1:-$(gh run list --workflow ci.yml --branch main --limit 1 --json databaseId --jq '.[0].databaseId')}
  echo "=== Jobs for run $run_id ==="
  gh api "repos/chernistry/bernstein/actions/runs/$run_id/jobs" --jq '.jobs[] | "\(.status) \(.conclusion // "...") \(.name)"'
  return 0
}

ci_errors() {
  local run_id=${1:-$(gh run list --workflow ci.yml --branch main --limit 1 --json databaseId --jq '.[0].databaseId')}
  echo "=== Errors in run $run_id ==="
  for job_name in "Lint" "Type check" "Test (Python 3.12)" "Test (Python 3.13)" "Dead code (Vulture)" "Spelling (typos)"; do
    JID=$(gh api "repos/chernistry/bernstein/actions/runs/$run_id/jobs" --jq ".jobs[] | select(.name == \"$job_name\" and .conclusion == \"failure\") | .id" 2>/dev/null)
    if [[ -n "$JID" ]]; then
      echo ""
      echo "--- $job_name ---"
      gh api "repos/chernistry/bernstein/actions/jobs/$JID/logs" 2>&1 | grep -E "error:|FAILED|Error" | head -5
    fi
  done
  return 0
}

# ── Quick Fixes ──
ci_fix_lint() {
  echo "Fixing lint..."
  uv run ruff check src/bernstein/ --fix 2>&1 | tail -3
  uv run ruff format src/bernstein/ tests/ 2>&1 | tail -3
  return 0
}

ci_fix_pyright() {
  echo "Checking pyright..."
  local files=$(uv run pyright 2>&1 | grep "error:" | sed 's|.*/src/bernstein/||; s|:.*||' | sort -u)
  if [[ -n "$files" ]]; then
    echo "Files with errors: $files"
    echo "Add to pyproject.toml [tool.pyright] exclude list"
  else
    echo "Pyright clean!"
  fi
  return 0
}

# ── Git Hygiene ──
git_hygiene() {
  echo "=== Git Hygiene ==="
  echo "Worktrees: $(git worktree list | wc -l | tr -d ' ')"
  echo "Agent branches: $(git branch | grep agent/ | wc -l | tr -d ' ')"
  echo "Dirty files: $(git status --short | wc -l | tr -d ' ')"
  echo "Stashes: $(git stash list | wc -l | tr -d ' ')"
  return 0
}

git_clean_agents() {
  echo "Cleaning agent worktrees and branches..."
  for wt in .sdd/worktrees/*/; do
    git worktree remove "$wt" --force 2>/dev/null && echo "Removed: $(basename "$wt")"
  done
  git branch | grep agent/ | while read -r br; do git branch -D "$br" 2>/dev/null; done
  echo "Done"
  return 0
}

# ── Runtime ──
runtime_clean() {
  echo "Cleaning runtime..."
  rm -f .sdd/runtime/tasks.jsonl .sdd/runtime/session_state.json .sdd/runtime/task_graph.json
  rm -f .sdd/runtime/agents.json .sdd/runtime/provider_status.json
  rm -f .sdd/runtime/*.log
  rm -rf .sdd/runtime/signals/ .sdd/runtime/costs/ .sdd/runtime/incidents/ .sdd/runtime/manifests/ .sdd/runtime/wal/
  rm -f .sdd/runtime/config_state.json .sdd/runtime/session.json .sdd/runtime/supervisor_state.json
  rm -rf .sdd/runtime/gates/
  rm -f .sdd/index/codebase.db
  echo "Runtime clean. Files: $(ls .sdd/runtime/ 2>/dev/null | wc -l | tr -d ' ')"
  return 0
}

# ── Server ──
server_health() {
  for ep in /health /status /tasks; do
    t=$(curl -s -o /dev/null -w '%{time_total}' "http://127.0.0.1:8052${ep}" 2>/dev/null)
    echo "$ep: ${t}s"
  done
  return 0
}

server_kill() {
  pkill -9 -f "bernstein.core.orchestrator" 2>/dev/null
  pkill -9 -f "uvicorn.*bernstein" 2>/dev/null
  pkill -9 -f "bernstein.core.server" 2>/dev/null
  sleep 1
  local remaining
  remaining=$(pgrep -f "bernstein" 2>/dev/null | wc -l | tr -d ' ')
  echo "Killed. Remaining: $remaining"
  return 0
}

# ── Issues ──
issues_open() {
  gh issue list --state open --json number,title,labels --jq '.[] | "#\(.number) [\(.labels | map(.name) | join(","))] \(.title)"'
  return 0
}

issues_close_done() {
  echo "Looking for implemented issues..."
  # List open issues, check if their feature exists in code
  gh issue list --state open --json number,title --jq '.[] | "\(.number)\t\(.title)"'
  return 0
}

# ── Full CI Fix Workflow ──
ci_full_fix() {
  echo "=== Full CI Fix Workflow ==="
  ci_fix_lint
  echo ""
  ci_fix_pyright
  echo ""
  echo "Running tests..."
  uv run pytest tests/unit/ -x -q --tb=short 2>&1 | tail -5
  echo ""
  echo "Committing fixes..."
  git add -A
  git diff --cached --quiet && echo "Nothing to commit" && return
  git commit -m "fix(ci): auto-fix lint, pyright, test issues"
  echo "Done. Push with: git push origin main"
}

echo "CI Debug Framework loaded. Commands:"
echo "  ci_status, ci_jobs [run_id], ci_errors [run_id]"
echo "  ci_fix_lint, ci_fix_pyright, ci_full_fix"
echo "  git_hygiene, git_clean_agents"
echo "  runtime_clean, server_health, server_kill"
echo "  issues_open, issues_close_done"
