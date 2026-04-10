# The Agentic Engineering Manifesto

*A framework for building reliable, autonomous, and scalable agent systems.*

## The Discipline of Agentic Engineering
We are moving beyond the era of narrow LLM wrappers and "co-pilots." The future belongs to **autonomous AI agents** capable of executing complex workflows, making decisions, and acting on our behalf. However, to trust agents with mission-critical systems, we must transition from *prompt engineering* to **Agentic Engineering**—a rigorous engineering discipline. 

Agentic Engineering treats AI agents not as magic black boxes, but as distributed system components that require deterministic orchestration, strict quality gates, and verifiable behaviors.

## Core Principles

### 1. Deterministic Scheduling Over Magic
Do not allow agents to free-roam without a map. Agents must operate within a clearly defined **task graph** (a Dependency DAG). The orchestrator handles task routing, scheduling, and error recovery—agents only execute the specific scope they are given.

### 2. Verify Output, Not Just Input
Garbage in, garbage out—but also, brilliant input can still result in garbage out. We must implement **Quality Gates** (linting, type-checking, semantic intent verification, and security scanning) that automatically review and reject poor agent output *before* it merges into the main state.

### 3. Provider-Agnostic Agility
Never lock your architecture to a single foundation model provider. A robust agentic system routes tasks to the best, fastest, or cheapest model dynamically based on the specific job requirements (e.g., Sonnet for reasoning, Haiku for parsing, 4o for specific tooling). 

### 4. Human Oversight and Approvals
Autonomy is a gradient. High-risk actions (e.g., executing untested code, spending money, or mutating production data) require explicit **Risk-Based Approval Workflows**. Humans remain in the loop for critical decisions, enabling trust through verifiable guardrails.

### 5. Intent Over Implementation
When verifying an agent's work, we must validate that the **Intent** of the prompt was satisfied. Does the code actually solve the original user requirement? Cross-model verification (using a different model to review the work of another) is the highest-signal quality improvement we can implement.

---

*Written by the Bernstein Contributors.*
*We build Bernstein to embody these principles.*
