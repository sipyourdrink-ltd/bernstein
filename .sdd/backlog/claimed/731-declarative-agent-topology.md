# 731 — Declarative Agent Topology in bernstein.yaml

**Role:** backend
**Priority:** 2 (high)
**Scope:** small
**Depends on:** none

## Problem

Agent roles, delegation rules, and access permissions are implicit — hardcoded in role prompts and spawner logic. There's no single place to see "who can delegate to whom" or "what tools does each role have access to." This makes the orchestration opaque and hard to audit. Industry patterns (KAOS, AWS Strands graph agents) show that declarative topology is both more auditable and more flexible.

## Design

### Topology section in bernstein.yaml
```yaml
topology:
  manager:
    model: opus
    effort: max
    delegates_to: [backend, qa, security, docs]
    max_reasoning_steps: 50
    tools: [git, shell]
  backend:
    model: sonnet
    effort: high
    delegates_to: []  # leaf agent, no delegation
    tools: [git, shell, mcp:github]
  qa:
    model: sonnet
    effort: high
    delegates_to: []
    tools: [git, shell, pytest]
  security:
    model: opus
    effort: max
    delegates_to: []
    tools: [git, shell]
```

### Validation
On startup, the orchestrator validates the topology:
- No circular delegation
- All referenced roles have definitions
- Model names resolve to known adapters

### Visualization
`bernstein topology` prints the delegation tree:
```
manager (opus/max)
├── backend (sonnet/high)
├── qa (sonnet/high)
├── security (opus/max)
└── docs (haiku/normal)
```

## Files to modify

- `src/bernstein/core/models.py` (TopologyConfig dataclass)
- `src/bernstein/core/spawner.py` (read topology for delegation rules)
- `src/bernstein/cli/main.py` (topology command)
- `tests/unit/test_topology.py` (new)

## Completion signal

- Topology defined in bernstein.yaml
- Spawner validates delegation rules
- `bernstein topology` prints the tree
