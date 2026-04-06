#!/usr/bin/env bash
# Fetch Bernstein orchestrator dashboard data
set -euo pipefail

API="${BERNSTEIN_API_URL:-http://127.0.0.1:8052}"
TOKEN="${BERNSTEIN_API_TOKEN:-}"

HEADERS=(-H "Content-Type: application/json")
if [[ -n "$TOKEN" ]]; then
  HEADERS+=(-H "Authorization: Bearer $TOKEN")
fi

# Try dashboard endpoint first (richest data)
RESPONSE=$(curl -sf "${HEADERS[@]}" "$API/dashboard/data" 2>/dev/null) || {
  # Fallback to /status
  RESPONSE=$(curl -sf "${HEADERS[@]}" "$API/status" 2>/dev/null) || {
    echo '{"error": "Bernstein API not reachable at '"$API"'. Start it with: bernstein run"}'
    exit 1
  }
}

echo "$RESPONSE"
