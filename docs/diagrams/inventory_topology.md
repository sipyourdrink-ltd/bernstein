# Inventory topology

<!-- AUTO-GENERATED from tests/fixtures/govern/inventory-store.json. Do not edit by hand. -->

The committed reference render is **mermaid** — GitHub and MkDocs display it
without Graphviz. `bernstein govern inventory --render` also ships `dot` on
demand. CI fails when this fence drifts from
`render_inventory(fixture, "mermaid")`.

```mermaid
flowchart TD
    agent_claude["Claude Code"]
    agent_codex["Codex"]
    host_dev["developer machine"]
    mcp_github["GitHub MCP"]
    skill_review["Review skill"]
    agent_claude --> mcp_github
    agent_claude --> skill_review
    host_dev --> agent_claude
    host_dev --> agent_codex
```
