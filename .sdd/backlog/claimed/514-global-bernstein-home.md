# 514 — Global ~/.bernstein home directory for cross-project state

**Role:** backend
**Priority:** 3
**Scope:** small
**Complexity:** low

## Problem
All state lives in `.sdd/` per-project. But some things are cross-project: user preferences, global agent catalog cache, API key configuration, default adapter selection, cost history across projects. Currently each project re-discovers everything from scratch.

## Implementation

### 1. `~/.bernstein/` structure
```
~/.bernstein/
├── config.yaml          # global defaults (cli, model, effort, budget)
├── agents/              # user-level agent definitions
│   └── catalog_cache.json  # cached catalog across projects
├── credentials.yaml     # API key references (not values — use env vars)
├── metrics/             # cross-project cost tracking
│   └── global_costs.jsonl
└── mcp/                 # cached MCP server installations
```

### 2. Config precedence
`project .sdd/config.yaml` > `project bernstein.yaml` > `~/.bernstein/config.yaml` > defaults

### 3. `bernstein config` command
```bash
bernstein config set cli claude      # global default adapter
bernstein config set budget 10.00    # global default budget
bernstein config get cli             # show effective value + source
bernstein config list                # show all with precedence
```

### 4. First-run creation
`bernstein init` or first run creates `~/.bernstein/` if it doesn't exist, with sensible defaults and a comment explaining each setting.

## Files
- src/bernstein/core/home.py (new) — BernsteinHome, config resolution
- src/bernstein/cli/main.py — add `config` command group
- src/bernstein/core/seed.py — merge global config with project config
- tests/unit/test_home.py (new)

## Completion signals
- file_contains: src/bernstein/core/home.py :: BernsteinHome
