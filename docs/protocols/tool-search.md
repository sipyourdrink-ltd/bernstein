# MCP tool-search lazy loading

When the MCP catalog gets large, every freshly-spawned agent receives
the full tool description in its system prompt - easily 67 k+ tokens
across 7+ servers. **Tool search** swaps that for a `tool_search`
meta-tool plus a compact name + one-line summary directory. Full JSON
schemas load on demand when the agent calls `tool_search(query)` and
follows up with `expand_tools([...])`.

## Why it exists

Bernstein's whole model is short-lived agents, fresh per task. Paying
67 k tokens per spawn just to *describe* tools the agent might never
use is the dominant cost on small tasks. Tool search keeps the
description budget bounded by the agent's actual need, not the
catalog's total surface area.

## How it triggers

Above a configurable token threshold, the agent's prompt contains:

```
Available tools (search to expand):
- tool_search(query: str) -> {names, summaries}
- expand_tools(names: list[str]) -> {schemas}

Compact directory (217 tools, full schemas available via expand_tools):
- gh.issue_create - open a GitHub issue
- gh.issue_comment - post a comment
- pg.query - run a SQL query against the configured Postgres
- ...
```

Below the threshold, the agent gets the full catalog in-prompt as
before. The behaviour is automatic; the agent does not need to know.

## How to use it

Default-on, no config required. To tune:

```yaml
# bernstein.yaml
mcp:
  tool_search:
    enabled: true                  # default
    threshold_tokens: 6000         # below this, ship full catalog
```

To inspect the search engine directly:

```python
from bernstein.core.protocols.mcp.mcp_tool_search import (
    ToolCatalog,
    ToolSearchEngine,
)

catalog = ToolCatalog.from_registered_servers()
engine = ToolSearchEngine(catalog)

hits = engine.search("git diff", k=10)
for hit in hits:
    print(hit.name, hit.summary, hit.score)

schemas = engine.expand_tools(["gh.diff", "git.diff"])
```

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `defaults.MCP_TOOL_SEARCH_ENABLED` | `true` | Master switch. |
| `defaults.MCP_TOOL_SEARCH_THRESHOLD_TOKENS` | `6000` | Total catalog token budget; above this, switch to tool_search. |
| Ranker | BM25 over `name + summary` | Lexical ranking only. |

Metric: `mcp_tool_search_invocations_total{outcome}` -
`hit` / `miss` / `expand`.

## Limitations

- BM25 (lexical) ranking only.
- Tool deduplication across servers (two MCP servers with the same
  tool name) is handled by `mcp_tool_normalization.py`, not by this
  module.
- Schemas returned by `expand_tools` count against the agent's
  context budget at expansion time. Expanding 50 schemas at once is
  not free.
- The threshold is a global token count.

## Related

- Source: `src/bernstein/core/protocols/mcp/mcp_tool_search.py`
- MCP manager: `src/bernstein/core/protocols/mcp/mcp_manager.py`
- [MCP server injection](../integrations/mcp-server-injection.md)
