# Signal Check Instructions

Every 60 seconds while working, check for orchestrator signal files:

```bash
cat .sdd/runtime/signals/{SESSION_ID}/WAKEUP 2>/dev/null
cat .sdd/runtime/signals/{SESSION_ID}/SHUTDOWN 2>/dev/null
```

## If SHUTDOWN exists

Save work and exit immediately:

```bash
git add -A && git commit -m "[WIP] {TASK_TITLE}" 2>/dev/null || true
exit 0
```

## If WAKEUP exists

Read the file, address the concern (save progress, report status), then continue working.

## Writing heartbeats

Every 30 seconds, write a heartbeat so the orchestrator knows you are alive:

```bash
cat > .sdd/runtime/heartbeats/{SESSION_ID}.json <<EOF
{"timestamp": $(date +%s), "files_changed": {FILES_CHANGED}, "status": "working", "current_file": "{CURRENT_FILE}"}
EOF
```
