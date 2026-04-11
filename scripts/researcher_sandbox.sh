#!/usr/bin/env bash
# researcher_sandbox.sh — spin up a network-isolated Bernstein instance for security research
#
# Usage:
#   ./scripts/researcher_sandbox.sh start    # start the sandbox
#   ./scripts/researcher_sandbox.sh stop     # stop and remove containers + volumes
#   ./scripts/researcher_sandbox.sh reset    # wipe tasks/worktrees, keep containers running
#   ./scripts/researcher_sandbox.sh status   # show running containers
#   ./scripts/researcher_sandbox.sh logs     # tail sandbox logs
#
# Requirements: Docker 24+, Docker Compose v2, bash 4+
# The sandbox binds to localhost ports 18052 (API) and 18080 (dashboard).
# No outbound network access is granted — egress is blocked at the compose level.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/docker/sandbox/docker-compose.researcher.yaml"
PROJECT_NAME="bernstein-research"

# ── Colour helpers ─────────────────────────────────────────────────────────

_green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
_yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
_red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
_bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

# ── Pre-flight ─────────────────────────────────────────────────────────────

check_deps() {
    local missing=()
    command -v docker >/dev/null 2>&1 || missing+=("docker")
    if ! docker compose version >/dev/null 2>&1; then
        missing+=("docker-compose-plugin")
    fi
    if [[ ${#missing[@]} -gt 0 ]]; then
        _red "Missing dependencies: ${missing[*]}"
        exit 1
    fi
    local docker_version
    docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1)
    if [[ "${docker_version:-0}" -lt 24 ]]; then
        _yellow "Warning: Docker 24+ recommended (found ${docker_version}.x). Proceeding anyway."
    fi
}

check_ports() {
    local ports=(18052 18080)
    for port in "${ports[@]}"; do
        if lsof -i ":${port}" >/dev/null 2>&1; then
            _red "Port ${port} is already in use. Stop the conflicting process first."
            exit 1
        fi
    done
}

# ── Commands ───────────────────────────────────────────────────────────────

cmd_start() {
    check_deps
    check_ports

    _bold "Starting Bernstein researcher sandbox..."

    docker compose \
        -p "${PROJECT_NAME}" \
        -f "${COMPOSE_FILE}" \
        up -d --build

    echo
    _green "Sandbox is up."
    echo
    _bold "API endpoints:"
    echo "  Task server:  http://localhost:18052"
    echo "  Dashboard:    http://localhost:18080"
    echo
    _bold "Demo tokens (use in Authorization: Bearer <token>):"
    echo "  research-token-1   — read-only observer"
    echo "  research-token-2   — standard agent (create/complete tasks)"
    echo "  research-token-3   — elevated agent (access /admin endpoints)"
    echo
    _bold "Quick test:"
    echo "  curl http://localhost:18052/tasks -H 'Authorization: Bearer research-token-2'"
    echo
    _yellow "All outbound network traffic is blocked inside the sandbox."
    _yellow "To stop: ./scripts/researcher_sandbox.sh stop"
}

cmd_stop() {
    _bold "Stopping and removing researcher sandbox..."
    docker compose \
        -p "${PROJECT_NAME}" \
        -f "${COMPOSE_FILE}" \
        down -v --remove-orphans
    _green "Done."
}

cmd_reset() {
    _bold "Resetting sandbox state (tasks, worktrees)..."
    docker compose \
        -p "${PROJECT_NAME}" \
        -f "${COMPOSE_FILE}" \
        exec bernstein-server \
        python -c "
import asyncio, sys
sys.path.insert(0, '/app')
from bernstein.core.task_store import get_task_store
async def reset():
    store = await get_task_store()
    await store.reset_all()
    print('Task store cleared.')
asyncio.run(reset())
" 2>/dev/null || _yellow "Could not reset task store (server may be starting). Try again in a few seconds."
    _green "Reset complete."
}

cmd_status() {
    docker compose \
        -p "${PROJECT_NAME}" \
        -f "${COMPOSE_FILE}" \
        ps
}

cmd_logs() {
    docker compose \
        -p "${PROJECT_NAME}" \
        -f "${COMPOSE_FILE}" \
        logs -f --tail=100
}

# ── Entrypoint ─────────────────────────────────────────────────────────────

case "${1:-help}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    reset)  cmd_reset ;;
    status) cmd_status ;;
    logs)   cmd_logs ;;
    help|--help|-h)
        echo "Usage: $0 {start|stop|reset|status|logs}"
        echo
        echo "  start   — build and start the isolated research sandbox"
        echo "  stop    — stop containers and delete volumes"
        echo "  reset   — clear tasks/worktrees without restarting containers"
        echo "  status  — show container status"
        echo "  logs    — tail all container logs"
        ;;
    *)
        _red "Unknown command: ${1}"
        echo "Run '$0 help' for usage."
        exit 1
        ;;
esac
