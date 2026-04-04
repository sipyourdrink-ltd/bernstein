#!/bin/bash
# Bernstein health monitor — prints compact status line every tick.
# Exits with non-zero + diagnostic message if something is wrong.
# Designed to be called by Claude Code in a loop.

set -euo pipefail
cd "$(dirname "$0")/.."

WARN=""
CRIT=""

# 1. Server alive?
STATUS=$(curl -s --max-time 10 http://127.0.0.1:8052/status 2>/dev/null || echo "DEAD")
if [ "$STATUS" = "DEAD" ]; then
    echo "CRITICAL: task server unreachable"
    exit 2
fi

# Parse server status
OPEN=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('open',0))" 2>/dev/null || echo "?")
CLAIMED=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('claimed',0))" 2>/dev/null || echo "?")
DONE=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('done',0))" 2>/dev/null || echo "?")
FAILED=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('failed',0))" 2>/dev/null || echo "?")

# 2. Agent count from last orchestrator log line
NOW=$(date +%s)
AGENT_COUNT=$(tail -1 .sdd/runtime/orchestrator.log 2>/dev/null | grep -o "agents=[0-9]*" | cut -d= -f2 || echo "?")
[ -z "$AGENT_COUNT" ] && AGENT_COUNT="?"

# 3. Disk space
FREE_GB=$(df -g . 2>/dev/null | tail -1 | awk '{print $4}' || echo "?")
if [ "$FREE_GB" != "?" ] && [ "$FREE_GB" -lt 2 ] 2>/dev/null; then
    CRIT="${CRIT}disk_low(${FREE_GB}GB) "
fi

# 4. Worktree count
WT_COUNT=$(ls -d .sdd/worktrees/*/ 2>/dev/null | wc -l | tr -d ' ')
if [ "$WT_COUNT" -gt 10 ]; then
    WARN="${WARN}worktrees(${WT_COUNT}) "
fi

# 5. Recent incidents
RECENT_INCIDENTS=$(grep "INCIDENT" .sdd/runtime/orchestrator-debug.log 2>/dev/null | tail -1 | grep -c "$(date +%H:)" || echo 0)

# 6. Orchestrator ticking? Check ALL log files for recent activity
ORCH_FRESH=0
for logf in .sdd/runtime/orchestrator*.log; do
    [ -f "$logf" ] || continue
    LT=$(stat -f %m "$logf" 2>/dev/null || stat -c %Y "$logf" 2>/dev/null || echo 0)
    if [ "$((NOW - LT))" -lt 120 ]; then
        ORCH_FRESH=1
        break
    fi
done
if [ "$ORCH_FRESH" -eq 0 ]; then
    CRIT="${CRIT}orchestrator_stale "
fi

# 7. Error rate from last orchestrator line
LAST_ERRORS=$(tail -1 .sdd/runtime/orchestrator.log 2>/dev/null | grep -o "errors=[0-9]*" | cut -d= -f2 || echo 0)

# Build status line
if [ -n "$CRIT" ]; then
    echo "CRITICAL: ${CRIT}| open=$OPEN claimed=$CLAIMED done=$DONE failed=$FAILED agents=$AGENT_COUNT wt=$WT_COUNT disk=${FREE_GB}GB"
    exit 2
elif [ -n "$WARN" ]; then
    echo "WARNING: ${WARN}| open=$OPEN claimed=$CLAIMED done=$DONE failed=$FAILED agents=$AGENT_COUNT wt=$WT_COUNT disk=${FREE_GB}GB"
    exit 1
else
    echo "OK | open=$OPEN claimed=$CLAIMED done=$DONE failed=$FAILED agents=$AGENT_COUNT wt=$WT_COUNT disk=${FREE_GB}GB"
    exit 0
fi
