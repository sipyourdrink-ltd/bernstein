## Team awareness

Other agents are working in parallel on this codebase. Recent activity:
{{ BULLETIN_SUMMARY }}

### Coordination rules
- Before creating a shared utility, check if it already exists: search the codebase first
- If you define an API endpoint, use consistent naming with existing endpoints in `src/bernstein/core/routes/`
- If you create a new module or important function, post it to the bulletin board so other agents know
- Do NOT modify files owned by other agents — if you need changes in their files, post a bulletin requesting it

### Bulletin board API
```bash
# Post a finding (new module, API, gotcha)
curl -s -X POST http://127.0.0.1:8052/bulletin \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "{{AGENT_ID}}", "type": "finding", "content": "Created src/foo/bar.py with FooClass"}'

# Post a blocker
curl -s -X POST http://127.0.0.1:8052/bulletin \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "{{AGENT_ID}}", "type": "blocker", "content": "Need changes in config.py owned by agent-xyz"}'

# Read recent bulletins
curl -s http://127.0.0.1:8052/bulletin?since=<unix_timestamp>
```
