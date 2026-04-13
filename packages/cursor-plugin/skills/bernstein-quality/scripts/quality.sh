#!/usr/bin/env bash
# Fetch Bernstein quality metrics
# Usage: quality.sh <metrics|pass-rates|times>
set -euo pipefail

API="${BERNSTEIN_API_URL:-http://127.0.0.1:8052}"
TOKEN="${BERNSTEIN_API_TOKEN:-}"

HEADERS=(-H "Content-Type: application/json")
if [[ -n "$TOKEN" ]]; then
  HEADERS+=(-H "Authorization: Bearer $TOKEN")
fi

ACTION="${1:-metrics}"

case "$ACTION" in
  metrics)
    curl -sf "${HEADERS[@]}" "$API/quality/metrics" || echo '{"error": "API not reachable"}' >&2
    ;;
  pass-rates)
    curl -sf "${HEADERS[@]}" "$API/quality/pass-rates" || echo '{"error": "API not reachable"}' >&2
    ;;
  times)
    curl -sf "${HEADERS[@]}" "$API/quality/completion-times" || echo '{"error": "API not reachable"}' >&2
    ;;
  *)
    echo "Unknown action: $ACTION"
    exit 1
    ;;
esac
