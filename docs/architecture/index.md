---
title: Architecture and internals
description: How Bernstein is built - system design, core concepts, orchestration internals, and the ADR trail.
search:
  boost: 2
---

# Architecture and internals

How Bernstein works under the hood, for readers extending it or
deciding whether its design fits their constraints.

<div class="grid cards" markdown>

- :material-sitemap-outline:{ .lg .middle } **System**

    ---

    Top-level architecture, task lifecycle, model routing, state persistence.

    [:octicons-arrow-right-24: Architecture](ARCHITECTURE.md)

- :material-lightbulb-outline:{ .lg .middle } **Concepts**

    ---

    The building blocks - fingerprint memoization, lineage trail, spec-as-test.

    [:octicons-arrow-right-24: Artifact lineage trail](../concepts/artifact-lineage.md)

- :material-graph-outline:{ .lg .middle } **Orchestration internals**

    ---

    Task DAG, run actor, worker coordination, failure taxonomy.

    [:octicons-arrow-right-24: Task DAG](../orchestration/task-dag.md)

- :material-gavel:{ .lg .middle } **Decisions (ADRs)**

    ---

    Why the orchestrator is deterministic, file-based, and LLM-free at the core.

    [:octicons-arrow-right-24: Why deterministic](WHY_DETERMINISTIC.md)

</div>
