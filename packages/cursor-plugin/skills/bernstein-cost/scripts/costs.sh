#!/usr/bin/env bash
# Fetch Bernstein cost data
# Usage: costs.sh [projection]
set -euo pipefail

API="${BERNSTEIN_API_URL:-http://127.0.0.1:8052}"
TOKEN="${BERNSTEIN_API_TOKEN:-}"

HEADERS=(-H "Content-Type: application/json")
if [[ -n "$TOKEN" ]]; then
  HEADERS+=(-H "Authorization: Bearer $TOKEN")
fi

ACTION="${1:-current}"

case "$ACTION" in
  projection)
    curl -sf "${HEADERS[@]}" "$API/costs/projection" || echo '{"error": "API not reachable"}' >&2
    ;;
  *)
    curl -sf "${HEADERS[@]}" "$API/costs" || echo '{"error": "API not reachable"}' >&2
    ;;
esac
