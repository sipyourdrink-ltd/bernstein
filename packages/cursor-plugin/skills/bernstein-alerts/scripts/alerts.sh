#!/usr/bin/env bash
# Fetch Bernstein alerts and diagnostics
set -euo pipefail

API="${BERNSTEIN_API_URL:-http://127.0.0.1:8052}"
TOKEN="${BERNSTEIN_API_TOKEN:-}"

HEADERS=(-H "Content-Type: application/json")
if [[ -n "$TOKEN" ]]; then
  HEADERS+=(-H "Authorization: Bearer $TOKEN")
fi

# Fetch alerts + dashboard data for comprehensive diagnostics
ALERTS=$(curl -sf "${HEADERS[@]}" "$API/alerts" 2>/dev/null || echo '[]')
DASHBOARD=$(curl -sf "${HEADERS[@]}" "$API/dashboard/data" 2>/dev/null || echo '{}')

python3 -c "
import json, sys

alerts = json.loads('''$ALERTS''')
dashboard = json.loads('''$DASHBOARD''')

result = {
    'alerts': alerts if isinstance(alerts, list) else alerts.get('alerts', []),
    'failed_tasks': [t for t in dashboard.get('tasks', []) if t.get('status') == 'failed'],
    'blocked_tasks': [t for t in dashboard.get('tasks', []) if t.get('status') == 'blocked'],
    'stalled_agents': [a for a in dashboard.get('agents', []) if a.get('status') == 'stalled'],
    'costs': dashboard.get('live_costs', {}),
}
print(json.dumps(result, indent=2))
" 2>/dev/null || echo '{"error": "API not reachable. Start Bernstein with: bernstein run"}' >&2
