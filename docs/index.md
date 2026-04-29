---
title: Bernstein — Open-Source Multi-Agent Orchestration Platform
description: >-
  Bernstein is the open-source multi-agent orchestrator for AI coding agents.
  Run Claude Code, Codex, Gemini CLI, and the OpenAI Agents SDK in parallel.
  Deterministic scheduling, 37 adapters, pluggable sandbox backends,
  cloud artifact storage, progressive skills, zero vendor lock-in.
tags:
  - orchestration
  - multi-agent
  - AI coding agents
---

# Bernstein

**Orchestrate any AI coding agent. Any model. One command.**

<figure markdown>
  ![Bernstein in action — parallel AI agents orchestrated in real time](assets/in-action-small.gif){ loading=lazy width="700" }
  <figcaption>Bernstein in action — parallel AI agents orchestrated in real time</figcaption>
</figure>

---

Bernstein takes a goal, breaks it into tasks, assigns them to AI coding agents running in parallel, verifies the output, and merges the results. You come back to working code, passing tests, and a clean git history.

No framework to learn. No vendor lock-in. Agents are interchangeable workers — swap any agent, any model, any provider. The orchestrator itself is deterministic Python code. Zero LLM tokens on scheduling.

## Install

=== "pip"

    ```bash
    pip install bernstein
    ```

=== "pipx"

    ```bash
    pipx install bernstein
    ```

=== "uv"

    ```bash
    uv tool install bernstein
    ```

=== "brew"

    ```bash
    brew install bernstein
    ```

Then run:

```bash
bernstein -g "Add JWT auth with refresh tokens, tests, and API docs"
```

## Why Bernstein?

<div class="grid cards" markdown>

- :material-speedometer:{ .lg .middle } **Deterministic scheduling**

    ---

    Pure Python orchestration — zero LLM tokens on coordination.
    Every decision is auditable code, not a model response.

- :material-swap-horizontal:{ .lg .middle } **Any agent, any model**

    ---

    37 CLI adapters: Claude Code, Codex, OpenAI Agents SDK v2, Gemini, Cursor, Aider, Cloudflare Agents, GitHub Copilot, Droid, Crush, and more.
    Mix cheap local models with cloud models in the same run.

- :material-source-branch:{ .lg .middle } **Git worktree isolation**

    ---

    Each agent works in its own git worktree.
    No merge conflicts. Clean history. Parallel by default.

- :material-shield-check:{ .lg .middle } **Built-in verification**

    ---

    Janitor system checks tests, lint, types, and PII
    before any agent output lands in your codebase.

</div>

## Quick links

| | |
|---|---|
| :material-rocket-launch: [Getting Started](getting-started/GETTING_STARTED.md) | Install and run your first orchestration |
| :material-wrench: [Configuration](operations/CONFIG.md) | bernstein.yaml reference |
| :material-puzzle: [Adapter Guide](adapters/ADAPTER_GUIDE.md) | Supported agents and how to add your own |
| :material-api: [API Reference](reference/openapi-reference.md) | Task server REST API |
| :material-sitemap: [Architecture](architecture/ARCHITECTURE.md) | How Bernstein works under the hood |
| :material-state-machine: [Lifecycle FSM](architecture/LIFECYCLE.md) | Task and agent state machines with transition tables |
| :material-text-box-check: [What's New](whats-new.md) | Summary of recent releases (1.8 → 1.9) |
| :material-history: [Changelog](CHANGELOG.md) | Full release history |

## Links

- [Website](https://bernstein.run)
- [GitHub](https://github.com/chernistry/bernstein)
- [PyPI](https://pypi.org/project/bernstein/)
- [npm](https://www.npmjs.com/package/bernstein-orchestrator)

---

Created by [Alex Chernysh](https://alexchernysh.com) ([@chernistry](https://github.com/chernistry)).
