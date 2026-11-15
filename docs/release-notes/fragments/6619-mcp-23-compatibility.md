## Preserve durable MCP tasks on the 2.3 SDK

Upgrade the MCP dependency to the tested 2.3 release line. Keep the existing
2025-11-25 task-create, polling, result, list and cancellation wire contract
backed by Bernstein's authenticated persistent TaskStore, despite removal of
the SDK's experimental task runtime. Ordinary SDK2 clients retain normal tool
calls. This does not implement or advertise the redesigned Tasks extension.

The compatibility adapter uses the SDK's provisional middleware API, so the
dependency is bounded below 2.4 until another compatibility review. Task
cancellation now supplies the existing HTTP endpoint's required JSON body.

The custom HTTP transport retains its four implemented handshake-era protocol
revisions. SDK2 clients in automatic mode negotiate the existing 2025-11-25
wire format; explicitly requested modern HTTP revisions are rejected until
that transport implements their response contract.
