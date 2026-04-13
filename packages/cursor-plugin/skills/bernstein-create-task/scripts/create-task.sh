#!/usr/bin/env bash
# Create a task in the Bernstein orchestrator
# Usage: create-task.sh "title" [role] [priority] [scope] [--require-review]
set -euo pipefail

API="${BERNSTEIN_API_URL:-http://127.0.0.1:8052}"
TOKEN="${BERNSTEIN_API_TOKEN:-}"

TITLE="${1:?Usage: create-task.sh \"title\" [role] [priority] [scope] [--require-review]}"
ROLE="${2:-backend}"
PRIORITY="${3:-1}"
SCOPE="${4:-small}"
REQUIRE_REVIEW=false

for arg in "$@"; do
  [[ "$arg" = "--require-review" ]] && REQUIRE_REVIEW=true
done

HEADERS=(-H "Content-Type: application/json")
if [[ -n "$TOKEN" ]]; then
  HEADERS+=(-H "Authorization: Bearer $TOKEN")
fi

BODY=$(cat <<EOF
{
  "title": "$TITLE",
  "role": "$ROLE",
  "priority": $PRIORITY,
  "scope": "$SCOPE",
  "require_review": $REQUIRE_REVIEW
}
EOF
)

curl -sf "${HEADERS[@]}" -X POST "$API/tasks" -d "$BODY" 2>/dev/null || {
  echo '{"error": "Failed to create task. Is Bernstein running? Start with: bernstein run"}' >&2
  exit 1
}
