#!/usr/bin/env bash
# Manage Bernstein approvals and plans
# Usage: approvals.sh <action> [id] [reason]
#   list              - List pending approvals
#   plans             - List pending plans
#   approve <id> [r]  - Approve task
#   reject <id> [r]   - Reject task
#   approve-plan <id> - Approve plan (promote planned tasks to open)
#   reject-plan <id>  - Reject plan (cancel planned tasks)
set -euo pipefail

API="${BERNSTEIN_API_URL:-http://127.0.0.1:8052}"
TOKEN="${BERNSTEIN_API_TOKEN:-}"

ACTION="${1:?Usage: approvals.sh <list|plans|approve|reject|approve-plan|reject-plan> [id] [reason]}"

HEADERS=(-H "Content-Type: application/json")
if [[ -n "$TOKEN" ]]; then
  HEADERS+=(-H "Authorization: Bearer $TOKEN")
fi

case "$ACTION" in
  list)
    curl -sf "${HEADERS[@]}" "$API/approvals" || echo '{"error": "API not reachable"}' >&2
    ;;
  plans)
    curl -sf "${HEADERS[@]}" "$API/plans?status=pending" || echo '{"error": "API not reachable"}' >&2
    ;;
  approve)
    ID="${2:?Task ID required}"
    REASON="${3:-Approved via Cursor plugin}"
    curl -sf "${HEADERS[@]}" -X POST "$API/approvals/$ID/approve" \
      -d "{\"reason\": \"$REASON\"}" || echo '{"error": "Failed to approve"}' >&2
    ;;
  reject)
    ID="${2:?Task ID required}"
    REASON="${3:-Rejected via Cursor plugin}"
    curl -sf "${HEADERS[@]}" -X POST "$API/approvals/$ID/reject" \
      -d "{\"reason\": \"$REASON\"}" || echo '{"error": "Failed to reject"}' >&2
    ;;
  approve-plan)
    ID="${2:?Plan ID required}"
    curl -sf "${HEADERS[@]}" -X POST "$API/plans/$ID/approve" || echo '{"error": "Failed to approve plan"}' >&2
    ;;
  reject-plan)
    ID="${2:?Plan ID required}"
    curl -sf "${HEADERS[@]}" -X POST "$API/plans/$ID/reject" || echo '{"error": "Failed to reject plan"}' >&2
    ;;
  *)
    echo "Unknown action: $ACTION"
    exit 1
    ;;
esac
