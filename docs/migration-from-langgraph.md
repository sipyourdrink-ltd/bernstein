# Migration Guide: From LangGraph to Bernstein

This guide helps teams migrate from LangGraph to Bernstein, mapping concepts and providing step-by-step migration instructions.

## Concept Mapping

| LangGraph Concept | Bernstein Equivalent | Notes |
|------------------|---------------------|-------|
| **Graph** | **Recipe** | Bernstein recipes define task workflows |
| **Node** | **Task** | Individual units of work |
| **Edge** | **Dependency** | `depends_on` field in tasks |
| **State** | **Task Context** | Shared via `.sdd/` file system |
| **Agent** | **Role + CLI Agent** | Bernstein separates role from execution |
| **Tool** | **Quality Gate** | Validation and verification |
| **Memory** | **Context Files** | `.sdd/` persistence |

## Architecture Differences

### LangGraph
```python
from langgraph.graph import StateGraph, END

workflow = StateGraph(State)
workflow.add_node("agent", agent_function)
workflow.add_edge("agent", END)
app = workflow.compile()
result = app.invoke({"input": "..."})
```

### Bernstein
```bash
# Define tasks in .sdd/backlog/open/
# Bernstein orchestrates automatically
bernstein run --goal "Build feature X"
```

## Migration Steps

### Step 1: Analyze Your LangGraph Setup

1. List all nodes and their functions
2. Document all edges and state transitions
3. Identify tools and memory usage

### Step 2: Map Nodes to Bernstein Tasks

LangGraph node:
```python
def code_review_node(state):
    # Review code
    return {"review": "..."}
```

Bernstein task (`.sdd/backlog/open/code-review.md`):
```markdown
---
id: "code-review"
title: "Review code changes"
role: qa
priority: 2
depends_on: ["implementation"]
---

## Description

Review the implemented code for quality and best practices.
```

### Step 3: Convert Graph to Recipe

LangGraph workflow:
```python
workflow.add_edge("planning", "implementation")
workflow.add_edge("implementation", "testing")
workflow.add_edge("testing", "review")
```

Bernstein recipe (`recipes/my-workflow.yaml`):
```yaml
id: my-workflow
title: My Development Workflow
steps:
  - id: planning
    role: manager
  - id: implementation
    role: backend
    depends_on: [planning]
  - id: testing
    role: qa
    depends_on: [implementation]
  - id: review
    role: security
    depends_on: [testing]
```

### Step 4: Migrate State Management

LangGraph:
```python
class State(TypedDict):
    input: str
    output: str
    intermediate: dict
```

Bernstein: State persists in `.sdd/` automatically
- Task results in `.sdd/backlog/closed/`
- Agent logs in `.sdd/runtime/logs/`
- Metrics in `.sdd/metrics/`

### Step 5: Replace Tools with Quality Gates

LangGraph tools:
```python
@tool
def run_tests(code: str) -> str:
    # Run tests
    return results
```

Bernstein quality gates (`.bernstein/quality_gates.yaml`):
```yaml
gates:
  - lint
  - tests
  - type_check
  - security_scan
```

### Step 6: Configure Agent Roles

LangGraph agents are custom functions. Bernstein uses specialist roles:

```yaml
# bernstein.yaml
team:
  - manager    # Task decomposition
  - backend    # Implementation
  - qa         # Testing
  - security   # Security review
  - devops     # Deployment
```

## Key Advantages of Bernstein

1. **No Code Required**: Define tasks in YAML/Markdown, not Python
2. **File-Based State**: `.sdd/` is git-friendly and inspectable
3. **Agent-Agnostic**: Works with Claude Code, Codex, Gemini, etc.
4. **Built-In Quality**: Quality gates run automatically
5. **Cost Tracking**: Per-task, per-role cost attribution

## Common Patterns

### Sequential Workflow (LangGraph) → Task Dependencies (Bernstein)

LangGraph:
```python
workflow.add_edge("step1", "step2")
workflow.add_edge("step2", "step3")
```

Bernstein:
```yaml
steps:
  - id: step1
  - id: step2
    depends_on: [step1]
  - id: step3
    depends_on: [step2]
```

### Conditional Edges (LangGraph) → Quality Gates (Bernstein)

LangGraph:
```python
def should_test(state):
    if state["code_quality"] > threshold:
        return "test"
    return "fix"
```

Bernstein: Quality gates automatically block merge on failure

### Parallel Execution (LangGraph) → Parallel Agents (Bernstein)

LangGraph:
```python
workflow.add_node("parallel1", func1)
workflow.add_node("parallel2", func2)
```

Bernstein: Set `max_agents: 6` for parallel execution

## Troubleshooting

### Issue: Tasks not executing
**Solution**: Check task status with `bernstein status`

### Issue: Dependencies not respected
**Solution**: Verify `depends_on` field in task files

### Issue: Quality gates failing
**Solution**: Review gate configuration in `.bernstein/quality_gates.yaml`

## Next Steps

1. Read the [Bernstein Documentation](README.md)
2. Explore example tasks in `.sdd/backlog/`
3. Try the TUI: `bernstein dashboard`
4. Join the community for support

## Support

- Documentation: `README.md`
- Issues: GitHub Issues
- Community: [Link to community]
