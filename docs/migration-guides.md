# Migration Guides

How to move from other orchestration frameworks to Bernstein.

## From CrewAI

### Key differences

| CrewAI | Bernstein |
|--------|-----------|
| Agents are LLM-based with role prompts | Agents are CLI processes (Claude Code, Codex, etc.) |
| Orchestration via LLM reasoning | Orchestration via deterministic code |
| State in memory | State in files (`.sdd/`) |
| Long-lived agent conversations | Short-lived agents (1-3 tasks, then exit) |
| Python API for defining crews | YAML plan files + CLI |
| Custom tools via Python functions | Agents use their own built-in tools |

### Migration steps

**1. Map your Crew to a plan file**

CrewAI:
```python
from crewai import Agent, Task, Crew

researcher = Agent(role="Researcher", goal="Research the topic")
writer = Agent(role="Writer", goal="Write the content")

research_task = Task(description="Research AI trends", agent=researcher)
write_task = Task(description="Write a blog post", agent=writer)

crew = Crew(agents=[researcher, writer], tasks=[research_task, write_task])
crew.kickoff()
```

Bernstein:
```yaml
# plans/blog-post.yaml
name: "Blog post pipeline"
stages:
  - name: research
    steps:
      - goal: "Research current AI trends and create notes in docs/research-notes.md"
        role: researcher
        priority: 1
        scope: ["docs/"]
        complexity: medium

  - name: writing
    depends_on: [research]
    steps:
      - goal: "Write a blog post based on docs/research-notes.md, output to docs/blog-post.md"
        role: docs
        priority: 2
        scope: ["docs/"]
        complexity: medium
```

**2. Replace custom tools with file-based communication**

CrewAI agents share data through tool calls and memory. Bernstein agents share data through files in the repository.

CrewAI:
```python
@tool
def search_web(query: str) -> str:
    return search_results
```

Bernstein approach: agents read and write files. If agent A produces output in `docs/research.md`, agent B reads it from disk.

**3. Replace delegation with stage dependencies**

CrewAI:
```python
crew = Crew(
    agents=[manager, worker],
    process=Process.hierarchical,
    manager_llm="gpt-4",
)
```

Bernstein:
```yaml
stages:
  - name: plan
    steps:
      - goal: "Break down the feature into subtasks and write them to docs/subtasks.md"
        role: manager
  - name: execute
    depends_on: [plan]
    steps:
      - goal: "Implement subtask 1"
        role: backend
      - goal: "Implement subtask 2"
        role: backend
```

**4. Replace memory with `.sdd/`**

CrewAI uses short-term (conversation) and long-term (vector store) memory. Bernstein uses:
- `.sdd/backlog/` for task state
- `.sdd/memory/` for cross-run knowledge
- `.sdd/bulletin/` for inter-agent messages within a run
- Repository files for agent output

## From LangGraph

### Key differences

| LangGraph | Bernstein |
|-----------|-----------|
| Graph-based agent workflows | Stage-based task orchestration |
| LLM decides control flow | Deterministic control flow |
| Checkpointing via state snapshots | WAL + file-based state |
| Python code defines graph | YAML plan files |
| Single process, async nodes | Multi-process, one per agent |
| Custom LLM chains | Standard CLI agents |

### Migration steps

**1. Map graph nodes to plan stages**

LangGraph:
```python
from langgraph.graph import StateGraph

graph = StateGraph(State)
graph.add_node("analyze", analyze_code)
graph.add_node("implement", implement_changes)
graph.add_node("test", run_tests)

graph.add_edge("analyze", "implement")
graph.add_edge("implement", "test")
graph.add_conditional_edges("test", check_results, {"pass": END, "fail": "implement"})
```

Bernstein:
```yaml
stages:
  - name: analyze
    steps:
      - goal: "Analyze the codebase and write findings to docs/analysis.md"
        role: architect
  - name: implement
    depends_on: [analyze]
    steps:
      - goal: "Implement changes based on docs/analysis.md"
        role: backend
  - name: test
    depends_on: [implement]
    steps:
      - goal: "Run tests and fix any failures"
        role: qa
```

**2. Replace conditional edges with quality gates**

LangGraph conditional routing is done by LLM or code. Bernstein uses quality gates:

```yaml
quality_gates:
  after_implement:
    - type: test
      command: "pytest tests/ -x"
    - type: lint
      command: "ruff check src/"
```

Failed quality gates automatically trigger retries.

**3. Replace state checkpoints with WAL**

LangGraph:
```python
graph.compile(checkpointer=SqliteSaver(conn))
```

Bernstein automatically maintains a WAL in `.sdd/runtime/wal/`. Recovery is automatic on restart.

**4. Replace tool definitions with agent capabilities**

LangGraph nodes often call custom tools. Bernstein agents (Claude Code, Codex, etc.) come with built-in tools for file editing, terminal commands, and web search. No custom tool definitions needed.

## From AutoGen

### Key differences

| AutoGen | Bernstein |
|---------|-----------|
| Multi-agent conversations | File-based task dispatch |
| GroupChat for coordination | Orchestrator tick loop + bulletin board |
| LLM-driven turn-taking | Priority-based queue |
| In-memory state | File-based `.sdd/` state |
| Python API | YAML plans + CLI |
| Agents are chat participants | Agents are isolated processes |

### Migration steps

**1. Replace GroupChat with plan files**

AutoGen:
```python
from autogen import AssistantAgent, UserProxyAgent, GroupChat, GroupChatManager

coder = AssistantAgent("coder", llm_config=config)
reviewer = AssistantAgent("reviewer", llm_config=config)
chat = GroupChat(agents=[coder, reviewer], messages=[])
manager = GroupChatManager(groupchat=chat)
user.initiate_chat(manager, message="Build a REST API")
```

Bernstein:
```yaml
stages:
  - name: code
    steps:
      - goal: "Build a REST API for the user service"
        role: backend
  - name: review
    depends_on: [code]
    steps:
      - goal: "Review the REST API implementation and fix issues"
        role: reviewer
```

**2. Replace agent conversations with file I/O**

AutoGen agents talk to each other in a chat thread. Bernstein agents communicate through:
- **Files in the repo**: Agent A writes code, Agent B reads and reviews it
- **Bulletin board**: Post cross-agent findings via the `/bulletin` API endpoint
- **Task metadata**: Progress reports attached to tasks

**3. Replace UserProxyAgent with the orchestrator**

AutoGen's UserProxyAgent acts as a human stand-in. In Bernstein, the orchestrator fills this role deterministically -- no LLM needed for coordination.

**4. Replace function calling with CLI agents**

AutoGen:
```python
@user.register_for_execution()
@coder.register_for_llm(description="Run tests")
def run_tests(test_file: str) -> str:
    return subprocess.run(["pytest", test_file], capture_output=True).stdout
```

Bernstein agents have built-in terminal access and can run any command directly.

## General migration tips

1. **Start with a simple plan.** Convert your simplest workflow first and verify it works before tackling complex multi-stage pipelines.

2. **Use file-based communication.** Instead of in-memory agent message passing, have agents write intermediate results to files that downstream agents read.

3. **Rely on the orchestrator.** Do not try to replicate LLM-based coordination. Bernstein's deterministic orchestrator handles task dispatch, retries, and resource management.

4. **Leverage existing CLI agents.** Do not rewrite custom tools. Claude Code, Codex, and other agents already know how to edit files, run tests, and use git.

5. **Use quality gates instead of LLM-based evaluation.** Instead of asking an LLM "is this good?", run concrete checks: tests pass, lint clean, type check clean.
