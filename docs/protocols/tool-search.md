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
MCP tool catalog is large; use the meta-tool below to load schemas on demand.
tool_search(query, limit=10): search the MCP tool directory by keyword. Returns ranked tool names + summaries. Call expand_tools(names=[...]) to fetch full JSON schemas before invoking.

Directory (names + 1-line summaries):
- gh.issue_create (gh): open a GitHub issue
- gh.issue_comment (gh): post a comment
- pg.query (pg): run a SQL query against the configured Postgres
- ...
... (N more tools, search to discover)
```

Below the threshold, the agent gets the full catalog in-prompt as
before. The behaviour is automatic; the agent does not need to know.

## How to use it

The lazy-loading prompt builder (`mcp_manager.build_tools_prompt_section`)
is not yet called from the spawn path. `tuning.mcp_tool_search.*` is
accepted by the config loader but has no effect today: the builder reads
the import-time `MCP_TOOL_SEARCH_ENABLED` / `MCP_TOOL_SEARCH_THRESHOLD_TOKENS`
constants and a fixed 1500-token directory budget. The accepted keys:

```yaml
# bernstein.yaml
tuning:
  mcp_tool_search:
    enabled: true                  # default
    threshold_tokens: 6000         # below this, ship full catalog
    directory_budget_tokens: 1500  # cap for the compact directory
```

To inspect the search engine directly:

```python
from bernstein.core.protocols.mcp.mcp_tool_search import (
    ToolCatalog,
    ToolEntry,
    ToolSearchEngine,
    expand_tools,
)

catalog = ToolCatalog([ToolEntry(name="gh.diff", summary="show a PR diff", server="gh", schema={})])
engine = ToolSearchEngine(catalog)

hits = engine.search("diff", limit=10)
for hit in hits:
    print(hit.name, hit.summary, hit.score)

schemas = expand_tools(catalog, ["gh.diff"])
```

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `defaults.MCP_TOOL_SEARCH_ENABLED` | `true` | Master switch. |
| `defaults.MCP_TOOL_SEARCH.directory_budget_tokens` | `1500` | Token cap for the compact directory after the swap. |
| `defaults.MCP_TOOL_SEARCH_THRESHOLD_TOKENS` | `6000` | Total catalog token budget; above this, switch to tool_search. |
| Ranker | BM25 over `name + summary` | Lexical ranking only. |

Metric: `mcp_tool_search_invocations_total{mode}` -
`search` / `expand`.

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
