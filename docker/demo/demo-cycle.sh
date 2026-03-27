#!/bin/bash
# Demo cycle — seeds tasks, runs real agents, resets every N minutes.
#
# Runs inside the bernstein-demo container.  Requires:
#   BERNSTEIN_SERVER_URL  — internal URL of bernstein-server (default: http://bernstein-server:8052)
#   ANTHROPIC_API_KEY or OPENAI_API_KEY — at least one LLM key
#   DEMO_CYCLE_INTERVAL   — seconds between resets (default: 900 = 15 min)
#
# The demo project lives at /workspace/project (Flask hello-world starter).
# Each cycle:
#   1. Reset the project to a clean state (git checkout)
#   2. Cancel any leftover tasks from the previous cycle
#   3. Seed 4 fresh tasks into the server
#   4. Start the bernstein orchestrator in the background
#   5. Wait DEMO_CYCLE_INTERVAL seconds
#   6. Stop the orchestrator and repeat

set -euo pipefail

SERVER="${BERNSTEIN_SERVER_URL:-http://bernstein-server:8052}"
INTERVAL="${DEMO_CYCLE_INTERVAL:-900}"
PROJECT_DIR="/workspace/project"

log() { echo "[demo-cycle] $(date -u '+%H:%M:%S') $*"; }

# ── Helpers ──────────────────────────────────────────────────────────────────

wait_for_server() {
    log "Waiting for server at ${SERVER}..."
    until curl -sf "${SERVER}/health" > /dev/null 2>&1; do
        sleep 3
    done
    log "Server ready."
}

cancel_all_active() {
    log "Cancelling leftover tasks..."
    for status in open claimed in_progress; do
        curl -sf "${SERVER}/tasks?status=${status}" 2>/dev/null \
        | python3 -c "
import sys, json
for t in json.load(sys.stdin):
    print(t['id'])
" 2>/dev/null \
        | while IFS= read -r task_id; do
            curl -sf -X POST "${SERVER}/tasks/${task_id}/cancel" \
                -H "Content-Type: application/json" \
                -d '{"reason": "demo reset"}' > /dev/null 2>&1 || true
        done
    done
}

post_task() {
    local title="$1"
    local description="$2"
    local role="${3:-backend}"
    local priority="${4:-2}"
    curl -sf -X POST "${SERVER}/tasks" \
        -H "Content-Type: application/json" \
        -d "{
            \"title\": $(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "${title}"),
            \"description\": $(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "${description}"),
            \"role\": \"${role}\",
            \"priority\": ${priority},
            \"scope\": \"small\",
            \"complexity\": \"low\"
        }" > /dev/null
}

seed_tasks() {
    log "Seeding demo tasks..."

    post_task \
        "Add /health endpoint" \
        "Add a GET /health endpoint to app.py that returns {\"status\": \"ok\", \"version\": \"1.0.0\"}. Keep it simple." \
        "backend" 1

    post_task \
        "Add /echo endpoint with input validation" \
        "Add a POST /echo endpoint that accepts JSON {\"message\": str}, validates that message is non-empty (return 400 if missing or blank), and echoes it back as {\"echo\": str}." \
        "backend" 2

    post_task \
        "Add global error handler" \
        "Add a Flask error handler for 404 (not found) and 500 (server error) that returns JSON {\"error\": str, \"status\": int} instead of HTML." \
        "backend" 3

    post_task \
        "Write unit tests" \
        "Add pytest tests in tests/test_app.py covering: GET / returns 200, GET /health returns {status: ok}, POST /echo round-trips a message, POST /echo with empty message returns 400." \
        "qa" 4

    log "Seeded 4 tasks."
}

ensure_git_repo() {
    # Initialize a git repo with an initial commit so reset_project can
    # restore the project to a clean state across cycles.
    if [ ! -d "${PROJECT_DIR}/.git" ]; then
        log "Initializing git repo for demo project..."
        git -C "${PROJECT_DIR}" init -b main
        git -C "${PROJECT_DIR}" config user.email "demo@bernstein.dev"
        git -C "${PROJECT_DIR}" config user.name "Bernstein Demo"
        git -C "${PROJECT_DIR}" add -A
        git -C "${PROJECT_DIR}" commit -m "Initial demo project"
    fi
}

reset_project() {
    log "Resetting demo project..."
    if [ -d "${PROJECT_DIR}/.git" ]; then
        git -C "${PROJECT_DIR}" checkout -- . 2>/dev/null || true
        git -C "${PROJECT_DIR}" clean -fd 2>/dev/null || true
    fi
}

# ── Main loop ─────────────────────────────────────────────────────────────────

wait_for_server
ensure_git_repo

CONDUCTOR_PID=""

cleanup() {
    if [ -n "${CONDUCTOR_PID}" ]; then
        log "Stopping orchestrator (PID ${CONDUCTOR_PID})..."
        kill "${CONDUCTOR_PID}" 2>/dev/null || true
        wait "${CONDUCTOR_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

while true; do
    log "=== Starting new demo cycle (interval: ${INTERVAL}s) ==="

    # Stop any previous orchestrator
    cleanup
    CONDUCTOR_PID=""

    # Reset state
    cancel_all_active
    reset_project
    seed_tasks

    # Start the orchestrator in the background.
    # Uses python -m bernstein.core.orchestrator so it connects to the existing
    # bernstein-server container (BERNSTEIN_SERVER_URL) without starting a new server.
    # max_agents and cli adapter come from bernstein.yaml in the project directory.
    log "Starting orchestrator..."
    cd "${PROJECT_DIR}"
    python -m bernstein.core.orchestrator &
    CONDUCTOR_PID=$!
    log "Orchestrator running (PID ${CONDUCTOR_PID})"

    # Wait for the cycle interval
    log "Next reset in ${INTERVAL}s. Dashboard: ${SERVER}/dashboard"
    sleep "${INTERVAL}"
done
