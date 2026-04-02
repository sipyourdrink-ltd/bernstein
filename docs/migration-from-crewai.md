# Migration Guide: CrewAI to Bernstein

This guide helps teams migrate from CrewAI to Bernstein, mapping concepts and providing step-by-step migration instructions.

## Concept Mapping

| CrewAI Concept | Bernstein Equivalent | Notes |
|----------------|---------------------|-------|
| **Agent** | **Role** + **CLI Agent** | Bernstein separates role definition from agent execution |
| **Task** | **Task** | Similar concept, Bernstein tasks have priority and complexity |
| **Process** | **Orchestrator** | Bernstein's orchestrator manages task lifecycle |
| **Crew** | **Recipe** | A collection of tasks to achieve a goal |
| **Tool** | **Quality Gate** | Bernstein uses quality gates for validation |
| **Memory** | **Context Files** | Shared via `.sdd/` file system |

## Architecture Differences

### CrewAI
```python
from crewai import Agent, Task, Crew

agent = Agent(role='developer', goal='write code', backstory='...')
task = Task(description='...', agent=agent)
crew = Crew(agents=[agent], tasks=[task])
result = crew.kickoff()
```

### Bernstein
```bash
# Define tasks in .sdd/backlog/open/
# Bernstein orchestrates automatically
bernstein run
```

## Migration Steps

### Step 1: Analyze Your CrewAI Setup

1. List all agents and their roles
2. Document all tasks and their dependencies
3. Identify tools and integrations used

### Step 2: Map Agents to Bernstein Roles

Bernstein provides built-in roles:
- `backend` - Backend development
- `frontend` - Frontend development
- `qa` - Quality assurance and testing
- `security` - Security review
- `devops` - DevOps and deployment
- `architect` - Architecture decisions
- `manager` - Task decomposition and planning

**Action**: Create a mapping document:
```
CrewAI Agent → Bernstein Role
developer → backend
tester → qa
devops-engineer → devops
```

### Step 3: Convert Tasks to Bernstein Format

CrewAI task:
```python
Task(
    description="Write API endpoint",
    expected_output="Working API endpoint",
    agent=developer_agent
)
```

Bernstein task (`.sdd/backlog/open/my-task.md`):
```markdown
---
id: "api-endpoint"
title: "Write API endpoint"
role: backend
priority: 2
complexity: medium
---

## Description

Write API endpoint for user registration.

## Expected Output

Working API endpoint with tests.
```

### Step 4: Define Your Goal as a Recipe

Create `bernstein.yaml` or use CLI:
```bash
bernstein run --goal "Build user registration API"
```

Bernstein will automatically decompose the goal into tasks.

### Step 5: Configure Quality Gates

CrewAI tools become Bernstein quality gates:

```yaml
# .bernstein/quality_gates.yaml
gates:
  - lint
  - type_check
  - tests
  - security_scan
```

### Step 6: Set Up Model Routing

Bernstein supports multiple models:
```yaml
# bernstein.yaml
model_routing:
  default: sonnet
  high_stakes: opus
  simple: haiku
```

### Step 7: Migrate Integrations

| CrewAI Integration | Bernstein Equivalent |
|-------------------|---------------------|
| LangChain tools | Quality gates |
| Custom tools | CLI adapters |
| API calls | MCP servers |

## Key Advantages of Bernstein

1. **Agent-Agnostic**: Works with Claude Code, Codex, Gemini, etc.
2. **Deterministic Orchestration**: Scheduling is code, not LLM
3. **File-Based State**: `.sdd/` is git-friendly and inspectable
4. **Self-Evolving**: Bernstein can improve itself via `bernstein evolve`
5. **Enterprise-Ready**: Approval gates, audit trails, cost tracking

## Common Patterns

### Sequential Tasks (CrewAI) → Task Dependencies (Bernstein)

CrewAI:
```python
task1 = Task(...)
task2 = Task(..., context=[task1])
```

Bernstein:
```yaml
# Task file
depends_on: ["task1-id"]
```

### Parallel Execution

CrewAI: Automatic with multiple agents
Bernstein: Automatic with `max_agents` configuration

```yaml
# bernstein.yaml
max_agents: 5  # Run up to 5 agents in parallel
```

### Conditional Logic

CrewAI: Custom code in task callbacks
Bernstein: Task dependencies and quality gates

## Troubleshooting

### Issue: Tasks not executing
**Solution**: Check task status with `bernstein status`

### Issue: Wrong model selected
**Solution**: Override in task file: `model: opus`

### Issue: Quality gates failing
**Solution**: Review gate configuration in `.bernstein/quality_gates.yaml`

## Next Steps

1. Read the [Bernstein Documentation](README.md)
2. Explore example tasks in `.sdd/backlog/`
3. Join the community for support
4. Consider contributing adapters or quality gates

## Support

- Documentation: `README.md`
- Issues: GitHub Issues
- Community: [Link to community]
