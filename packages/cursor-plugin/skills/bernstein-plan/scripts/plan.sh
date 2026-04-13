#!/usr/bin/env bash
# Manage Bernstein plans
# Usage: plan.sh <list|submit|show> [plan_file_or_id]
set -euo pipefail

API="${BERNSTEIN_API_URL:-http://127.0.0.1:8052}"
TOKEN="${BERNSTEIN_API_TOKEN:-}"

ACTION="${1:?Usage: plan.sh <list|submit|show> [plan_file_or_id]}"

HEADERS=(-H "Content-Type: application/json")
if [[ -n "$TOKEN" ]]; then
  HEADERS+=(-H "Authorization: Bearer $TOKEN")
fi

case "$ACTION" in
  list)
    curl -sf "${HEADERS[@]}" "$API/plans" || echo '{"error": "API not reachable"}' >&2
    ;;
  show)
    ID="${2:?Plan ID required}"
    curl -sf "${HEADERS[@]}" "$API/plans/$ID" || echo '{"error": "Plan not found"}' >&2
    ;;
  submit)
    FILE="${2:?Plan YAML file required}"
    if [[ ! -f "$FILE" ]]; then
      echo "{\"error\": \"File not found: $FILE\"}" >&2
      exit 1
    fi
    # Convert YAML to JSON and submit
    python3 -c "
import yaml, json, sys
with open('$FILE') as f:
    plan = yaml.safe_load(f)
print(json.dumps(plan))
" | curl -sf "${HEADERS[@]}" -X POST "$API/plans" -d @- || echo '{"error": "Failed to submit plan"}' >&2
    ;;
  *)
    echo "Unknown action: $ACTION"
    exit 1
    ;;
esac
