---
title: Operations
description: Running Bernstein in production - deployment, day-to-day supervision, reliability, cost, and compliance.
search:
  boost: 2
---

# Operations

The operator surface: deploying Bernstein, running it day to day, and
keeping it healthy, cheap, secure, and auditable.

<div class="grid cards" markdown>

- :material-rocket-launch-outline:{ .lg .middle } **Deploy and scale**

    ---

    Docker Compose, Helm, cluster mode, fleet, air-gap installs.

    [:octicons-arrow-right-24: Deployment guide](deployment-guide.md)

- :material-play-box-outline:{ .lg .middle } **Run and supervise**

    ---

    Run, schedule, resume, replay, fork, and review agent work.

    [:octicons-arrow-right-24: Commands overview](commands.md)

- :material-shield-refresh-outline:{ .lg .middle } **Reliability**

    ---

    Troubleshooting, retries, auto-heal, stall escalation, disaster recovery.

    [:octicons-arrow-right-24: Troubleshooting](TROUBLESHOOTING.md)

- :material-cash-multiple:{ .lg .middle } **Cost and performance**

    ---

    Budgets, cost-aware scheduling, performance tuning.

    [:octicons-arrow-right-24: Cost optimization](cost-optimization.md)

- :material-chart-line:{ .lg .middle } **Observability**

    ---

    Instrumentation, deterministic replay, telemetry, trends.

    [:octicons-arrow-right-24: Observability overview](observability-overview.md)

- :material-source-merge:{ .lg .middle } **Merge and review automation**

    ---

    Merge queue, autofix daemon, review responder, coverage ratchet.

    [:octicons-arrow-right-24: Merge queue](merge-queue.md)

- :material-clipboard-check-outline:{ .lg .middle } **Evaluation and calibration**

    ---

    Incident-to-eval synthesis, A/B runner, calibration.

    [:octicons-arrow-right-24: Incident-to-eval synthesis](../eval/incident-synthesis.md)

- :material-lock-outline:{ .lg .middle } **Security and identity**

    ---

    Credential scoping, secrets, hardening, capability matrix.

    [:octicons-arrow-right-24: Security and identity stack](security-and-identity.md)

- :material-file-check-outline:{ .lg .middle } **Compliance and audit**

    ---

    EU AI Act, SOC 2, audit log, lineage export.

    [:octicons-arrow-right-24: Compliance overview](compliance.md)

- :material-file-sign:{ .lg .middle } **Artifacts**

    ---

    Signed, content-addressed non-coding artifacts and `artifact verify`.

    [:octicons-arrow-right-24: Artifact contract](artifacts.md)

</div>
