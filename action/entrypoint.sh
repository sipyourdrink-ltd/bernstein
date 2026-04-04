#!/usr/bin/env bash
# Bernstein GitHub Action entrypoint.
# Handles four trigger modes:
#   fix-ci      — download failed CI logs, attempt a fix (workflow_run failure)
#   review-pr   — review an open pull request and post a comment
#   decompose   — decompose a labeled GitHub issue into agent tasks
#   <any text>  — run bernstein with the provided task description directly
set -euo pipefail

# ---------------------------------------------------------------------------
# Inputs (set by the composite action via env vars)
# ---------------------------------------------------------------------------
TASK="${INPUT_TASK:-}"
PLAN="${INPUT_PLAN:-}"
BUDGET="${INPUT_BUDGET:-5.00}"
CLI="${INPUT_CLI:-claude}"
MAX_RETRIES="${INPUT_MAX_RETRIES:-3}"
POST_COMMENT="${INPUT_POST_COMMENT:-true}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
gha_group()    { echo "::group::$1"; }
gha_endgroup() { echo "::endgroup::"; }
gha_notice()   { echo "::notice::$1"; }
gha_warning()  { echo "::warning::$1"; }
gha_error()    { echo "::error::$1"; }

ensure_config() {
    if [ ! -f bernstein.yaml ]; then
        gha_group "Creating default bernstein.yaml"
        cat > bernstein.yaml <<YAML
cli: ${CLI}
max_agents: 2
constraints:
  - "Run tests before marking tasks complete"
  - "Commit with descriptive messages after completing each task"
YAML
        gha_endgroup
    fi
}

# Emit step outputs to GITHUB_OUTPUT.
# Reads .sdd/run-summary.json if present; otherwise uses safe defaults.
emit_outputs() {
    local tasks_completed=0
    local total_cost="0.00"
    local pr_url=""
    local evidence_path=""

    if [ -f ".sdd/run-summary.json" ]; then
        tasks_completed=$(jq -r '.tasks_completed // 0'        .sdd/run-summary.json 2>/dev/null || echo 0)
        total_cost=$(jq -r      '.total_cost    // "0.00"'     .sdd/run-summary.json 2>/dev/null || echo "0.00")
        pr_url=$(jq -r          '.pr_url        // ""'         .sdd/run-summary.json 2>/dev/null || echo "")
    fi

    if [ -d ".sdd/evidence" ]; then
        evidence_path=".sdd/evidence"
    fi

    if [ -n "${GITHUB_OUTPUT:-}" ]; then
        echo "tasks_completed=${tasks_completed}"       >> "$GITHUB_OUTPUT"
        echo "total_cost=${total_cost}"                 >> "$GITHUB_OUTPUT"
        echo "pr_url=${pr_url}"                         >> "$GITHUB_OUTPUT"
        echo "evidence_bundle_path=${evidence_path}"    >> "$GITHUB_OUTPUT"
    fi

    gha_notice "Tasks completed: ${tasks_completed} | Cost: \$${total_cost}"
}

# Post a comment on the associated pull request (if running in a PR context).
post_pr_comment() {
    local body="$1"

    if [ "${POST_COMMENT}" != "true" ]; then
        return 0
    fi

    local pr_number=""

    case "${GITHUB_EVENT_NAME:-}" in
        pull_request|pull_request_target)
            pr_number=$(jq -r '.pull_request.number // ""' "${GITHUB_EVENT_PATH:-/dev/null}" 2>/dev/null || echo "")
            ;;
        workflow_run)
            # Find any open PR that matches the triggering commit's SHA.
            local head_sha
            head_sha=$(jq -r '.workflow_run.head_sha // ""' "${GITHUB_EVENT_PATH:-/dev/null}" 2>/dev/null || echo "")
            if [ -n "$head_sha" ]; then
                pr_number=$(gh pr list --state open \
                    --json number,headRefOid \
                    --jq ".[] | select(.headRefOid == \"${head_sha}\") | .number" \
                    2>/dev/null | head -1 || echo "")
            fi
            ;;
        issues)
            # Comment on the issue itself instead of a PR.
            local issue_number
            issue_number=$(jq -r '.issue.number // ""' "${GITHUB_EVENT_PATH:-/dev/null}" 2>/dev/null || echo "")
            if [ -n "$issue_number" ] && [ "$issue_number" != "null" ]; then
                gh issue comment "$issue_number" --body "$body" 2>/dev/null || true
            fi
            return 0
            ;;
    esac

    if [ -n "$pr_number" ] && [ "$pr_number" != "null" ]; then
        gh pr comment "$pr_number" --body "$body" 2>/dev/null || true
    fi
}

# Build the markdown comment body posted after a run.
build_comment() {
    local status="$1"   # success | failure
    local tasks_completed="${2:-0}"
    local total_cost="${3:-0.00}"

    local icon="✅"
    [ "$status" = "failure" ] && icon="❌"

    cat <<MARKDOWN
## ${icon} Bernstein Orchestration Summary

| Field | Value |
|-------|-------|
| Status | \`${status}\` |
| Tasks completed | ${tasks_completed} |
| Total cost | \$${total_cost} |
| Trigger | \`${TASK}\` |
| Run | [${GITHUB_RUN_ID:-—}](${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}) |

<details>
<summary>What is Bernstein?</summary>
Bernstein is a multi-agent orchestration system that hires short-lived CLI coding agents to complete tasks autonomously.
Learn more at https://github.com/chernistry/bernstein
</details>
MARKDOWN
}

# ---------------------------------------------------------------------------
# Fix-CI mode  (on: workflow_run — triggered when CI fails)
# ---------------------------------------------------------------------------
run_fix_ci() {
    gha_group "fix-ci: downloading failed job logs"

    local failed_logs=""
    local attempt=0

    if [ "${GITHUB_EVENT_NAME:-}" = "workflow_run" ]; then
        local trigger_run_id
        trigger_run_id=$(jq -r '.workflow_run.id // ""' "${GITHUB_EVENT_PATH:-/dev/null}" 2>/dev/null || echo "")
        if [ -n "$trigger_run_id" ] && [ "$trigger_run_id" != "null" ]; then
            failed_logs=$(gh run view "$trigger_run_id" --log-failed 2>/dev/null || true)
        fi
    fi

    # Fallback: most recent failed run on this branch.
    if [ -z "$failed_logs" ]; then
        local branch
        branch="$(git rev-parse --abbrev-ref HEAD)"
        local latest_failed
        latest_failed=$(gh run list --branch "$branch" --status failure --limit 1 \
            --json databaseId --jq '.[0].databaseId' 2>/dev/null || true)
        if [ -n "$latest_failed" ] && [ "$latest_failed" != "null" ]; then
            failed_logs=$(gh run view "$latest_failed" --log-failed 2>/dev/null || true)
        fi
    fi

    gha_endgroup

    local goal
    if [ -z "$failed_logs" ]; then
        gha_warning "No failed job logs found. Running bernstein with generic CI fix goal."
        goal="Fix failing CI checks. Run the test suite and linter, identify failures, and fix them."
    else
        local truncated_logs
        truncated_logs=$(echo "$failed_logs" | tail -n 200)
        goal="Fix the CI failure described in the logs below. Identify the root cause, apply a fix, and verify locally.

--- FAILED CI LOGS ---
${truncated_logs}
--- END LOGS ---"
    fi

    local status="success"
    while [ "$attempt" -lt "$MAX_RETRIES" ]; do
        attempt=$((attempt + 1))
        gha_group "fix-ci: attempt ${attempt}/${MAX_RETRIES}"
        echo "Attempt ${attempt}/${MAX_RETRIES}"

        if bernstein -g "$goal" --budget "$BUDGET" --headless; then
            echo "Bernstein completed on attempt ${attempt}."
            gha_endgroup
            status="success"
            break
        fi

        gha_endgroup

        if [ "$attempt" -lt "$MAX_RETRIES" ]; then
            gha_warning "Attempt ${attempt} failed. Retrying…"
        else
            gha_error "Bernstein failed after ${MAX_RETRIES} attempts."
            status="failure"
        fi
    done

    emit_outputs

    local tasks_completed=0 total_cost="0.00"
    [ -f ".sdd/run-summary.json" ] && {
        tasks_completed=$(jq -r '.tasks_completed // 0'    .sdd/run-summary.json 2>/dev/null || echo 0)
        total_cost=$(jq -r      '.total_cost    // "0.00"' .sdd/run-summary.json 2>/dev/null || echo "0.00")
    }
    post_pr_comment "$(build_comment "$status" "$tasks_completed" "$total_cost")"

    [ "$status" = "success" ]
}

# ---------------------------------------------------------------------------
# Plan mode  (bernstein run <plan-file>)
# ---------------------------------------------------------------------------
run_plan() {
    gha_group "Running Bernstein plan: ${PLAN}"
    echo "Plan:   ${PLAN}"
    echo "Budget: \$${BUDGET}"
    echo "CLI:    ${CLI}"

    if [ ! -f "$PLAN" ]; then
        gha_error "Plan file not found: ${PLAN}"
        gha_endgroup
        return 1
    fi

    local status="success"
    bernstein run "$PLAN" --budget "$BUDGET" --headless || status="failure"

    gha_endgroup

    emit_outputs

    local tasks_completed=0 total_cost="0.00"
    [ -f ".sdd/run-summary.json" ] && {
        tasks_completed=$(jq -r '.tasks_completed // 0'    .sdd/run-summary.json 2>/dev/null || echo 0)
        total_cost=$(jq -r      '.total_cost    // "0.00"' .sdd/run-summary.json 2>/dev/null || echo "0.00")
    }
    post_pr_comment "$(build_comment "$status" "$tasks_completed" "$total_cost")"

    [ "$status" = "success" ]
}

# ---------------------------------------------------------------------------
# Normal mode  (any task string, including review-pr and decompose)
# ---------------------------------------------------------------------------
run_normal() {
    gha_group "Running Bernstein"
    echo "Task:   ${TASK}"
    echo "Budget: \$${BUDGET}"
    echo "CLI:    ${CLI}"

    local status="success"
    bernstein -g "$TASK" --budget "$BUDGET" --headless || status="failure"

    gha_endgroup

    emit_outputs

    local tasks_completed=0 total_cost="0.00"
    [ -f ".sdd/run-summary.json" ] && {
        tasks_completed=$(jq -r '.tasks_completed // 0'    .sdd/run-summary.json 2>/dev/null || echo 0)
        total_cost=$(jq -r      '.total_cost    // "0.00"' .sdd/run-summary.json 2>/dev/null || echo "0.00")
    }
    post_pr_comment "$(build_comment "$status" "$tasks_completed" "$total_cost")"

    [ "$status" = "success" ]
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
ensure_config

# Validate: exactly one of task or plan must be set
if [ -z "$TASK" ] && [ -z "$PLAN" ]; then
    gha_error "Either 'task' or 'plan' input must be provided."
    exit 1
fi
if [ -n "$TASK" ] && [ -n "$PLAN" ]; then
    gha_error "Provide either 'task' or 'plan', not both."
    exit 1
fi

if [ -n "$PLAN" ]; then
    run_plan
elif [ "$TASK" = "fix-ci" ]; then
    run_fix_ci
else
    run_normal
fi
