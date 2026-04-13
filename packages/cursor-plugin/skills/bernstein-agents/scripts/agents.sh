#!/usr/bin/env bash
# Manage Bernstein agents
# Usage: agents.sh <list|logs|kill> [session_id]
set -euo pipefail

API="${BERNSTEIN_API_URL:-http://127.0.0.1:8052}"
TOKEN="${BERNSTEIN_API_TOKEN:-}"

ACTION="${1:?Usage: agents.sh <list|logs|kill> [session_id]}"

HEADERS=(-H "Content-Type: application/json")
if [[ -n "$TOKEN" ]]; then
  HEADERS+=(-H "Authorization: Bearer $TOKEN")
fi

case "$ACTION" in
  list)
    curl -sf "${HEADERS[@]}" "$API/dashboard/data" 2>/dev/null \
      | python3 -c "
import sys, json
d = json.load(sys.stdin)
agents = d.get('agents', [])
print(json.dumps({'agents': agents, 'count': len(agents)}, indent=2))
" || echo '{"error": "API not reachable"}' >&2
    ;;
  logs)
    ID="${2:?Session ID required}"
    curl -sf "${HEADERS[@]}" "$API/agents/$ID/logs?tail_bytes=4096" || echo '{"error": "Could not fetch logs"}' >&2
    ;;
  kill)
    ID="${2:?Session ID required}"
    curl -sf "${HEADERS[@]}" -X POST "$API/agents/$ID/kill" || echo '{"error": "Could not kill agent"}' >&2
    ;;
  *)
    echo "Unknown action: $ACTION"
    exit 1
    ;;
esac
