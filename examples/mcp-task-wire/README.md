# Plain and task-augmented MCP calls

Run this example from the repository root in the Bernstein development environment.

```sh
python examples/mcp-task-wire/demo.py
```

The client and server exchange MCP messages over in-memory streams. The
client negotiates the legacy `2025-11-25` handshake, checks the server's
task capability and reads the greeting tool's `execution.taskSupport`
field from `tools/list`.

The client calls `demo_greet` twice with the same arguments. A plain call
returns the greeting. Adding `"task": {"ttl": 86400000}` to the
`tools/call` parameters requests a task handle. Bernstein's
`legacy_tasks_middleware` restores the legacy response envelope from the
tool's structured result. The demo uses the SDK's JSON-RPC dispatcher to
read that response and validates it as a `CreateTaskResult`.

```text
Protocol: 2025-11-25
Tasks capability: True
Tool task support: optional
CallToolResult: hello, Ada
CreateTaskResult: demo-task-0001 working ttl=86400000
```

The handle contains fixed sample data. The example ends at task creation
and runs entirely in process. Run its tests with
`python -m pytest -q tests/unit/examples/test_mcp_task_wire_demo.py`.
