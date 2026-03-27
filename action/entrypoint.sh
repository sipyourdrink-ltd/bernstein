#!/usr/bin/env bash
# Bernstein GitHub Action entrypoint.
# Handles two modes:
#   1. fix-ci  — download failed CI logs, pass them to bernstein as context
#   2. normal  — run bernstein with the user-provided task description
set -euo pipefail

# ---------------------------------------------------------------------------
# Inputs (set by the composite action via env vars)
# ---------------------------------------------------------------------------
TASK="${INPUT_TASK:?INPUT_TASK is required}"
BUDGET="${INPUT_BUDGET:-5.00}"
CLI="${INPUT_CLI:-claude}"
MAX_RETRIES="${INPUT_MAX_RETRIES:-3}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { echo "::group::$1"; }
endlog() { echo "::endgroup::"; }

ensure_config() {
    # Create a minimal bernstein.yaml if one doesn't exist
    if [ ! -f bernstein.yaml ]; then
        log "Creating default bernstein.yaml"
        cat > bernstein.yaml <<YAML
cli: ${CLI}
max_agents: 2
constraints:
  - "Run tests before marking tasks complete"
  - "Commit with descriptive messages after completing each task"
YAML
        endlog
    fi
}

# ---------------------------------------------------------------------------
# Fix-CI mode
# ---------------------------------------------------------------------------
run_fix_ci() {
    log "fix-ci: downloading failed job logs"

    FAILED_LOGS=""
    ATTEMPT=0

    # Get the run ID of the triggering workflow (workflow_run event)
    if [ -n "${GITHUB_EVENT_NAME:-}" ] && [ "${GITHUB_EVENT_NAME}" = "workflow_run" ]; then
        TRIGGER_RUN_ID=$(jq -r '.workflow_run.id' "$GITHUB_EVENT_PATH")
        if [ "$TRIGGER_RUN_ID" != "null" ] && [ -n "$TRIGGER_RUN_ID" ]; then
            FAILED_LOGS=$(gh run view "$TRIGGER_RUN_ID" --log-failed 2>/dev/null || true)
        fi
    fi

    # Fallback: get the most recent failed run on this branch
    if [ -z "$FAILED_LOGS" ]; then
        BRANCH="$(git rev-parse --abbrev-ref HEAD)"
        LATEST_FAILED_RUN=$(gh run list --branch "$BRANCH" --status failure --limit 1 --json databaseId --jq '.[0].databaseId' 2>/dev/null || true)
        if [ -n "$LATEST_FAILED_RUN" ] && [ "$LATEST_FAILED_RUN" != "null" ]; then
            FAILED_LOGS=$(gh run view "$LATEST_FAILED_RUN" --log-failed 2>/dev/null || true)
        fi
    fi

    endlog

    if [ -z "$FAILED_LOGS" ]; then
        echo "::warning::No failed job logs found. Running bernstein with generic CI fix goal."
        GOAL="Fix failing CI checks. Run the test suite and linter, identify failures, and fix them."
    else
        # Truncate logs to avoid blowing up token limits (keep last 200 lines)
        TRUNCATED_LOGS=$(echo "$FAILED_LOGS" | tail -n 200)
        GOAL="Fix the CI failure described in the logs below. Identify the root cause, apply a fix, and verify locally.

--- FAILED CI LOGS ---
${TRUNCATED_LOGS}
--- END LOGS ---"
    fi

    log "fix-ci: running bernstein (attempt 1/${MAX_RETRIES})"

    while [ "$ATTEMPT" -lt "$MAX_RETRIES" ]; do
        ATTEMPT=$((ATTEMPT + 1))
        echo "Attempt ${ATTEMPT}/${MAX_RETRIES}"

        if bernstein -g "$GOAL" --budget "$BUDGET" --headless; then
            echo "Bernstein completed successfully on attempt ${ATTEMPT}."
            endlog
            return 0
        fi

        if [ "$ATTEMPT" -lt "$MAX_RETRIES" ]; then
            echo "::warning::Bernstein attempt ${ATTEMPT} failed. Retrying..."
        fi
    done

    endlog
    echo "::error::Bernstein failed after ${MAX_RETRIES} attempts."
    return 1
}

# ---------------------------------------------------------------------------
# Normal mode
# ---------------------------------------------------------------------------
run_normal() {
    log "Running bernstein with task"
    echo "Task: ${TASK}"
    echo "Budget: \$${BUDGET}"
    echo "CLI: ${CLI}"

    bernstein -g "$TASK" --budget "$BUDGET" --headless
    endlog
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
ensure_config

if [ "$TASK" = "fix-ci" ]; then
    run_fix_ci
else
    run_normal
fi
