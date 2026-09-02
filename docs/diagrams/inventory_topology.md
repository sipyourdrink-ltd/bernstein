# Inventory topology

<!-- AUTO-GENERATED from tests/fixtures/govern/inventory-store.json. Do not edit by hand. -->

The committed reference render is **mermaid** — GitHub and MkDocs display it
without Graphviz. `bernstein govern inventory --render` also ships `dot` on
demand. CI fails when this fence drifts from
`render_inventory(fixture, "mermaid")`.

```mermaid
flowchart TD
    n0["agent_claude: Claude Code"]
    n1["agent_codex: Codex"]
    n2["host_dev: developer machine"]
    n3["mcp_github: GitHub MCP"]
    n4["skill_review: Review skill"]
    n0 --> n3
    n0 --> n4
    n2 --> n0
    n2 --> n1
```
