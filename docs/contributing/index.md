---
title: Contribute
description: Sending a PR to Bernstein - code review, testing, adapter contracts, and release process.
search:
  boost: 2
---

# Contribute

What you need before sending a PR: how review works, how tests are
organized, and the contracts new adapters and hooks must satisfy.

<div class="grid cards" markdown>

- :material-source-pull:{ .lg .middle } **Code review and testing**

    ---

    How review works and how the test suite is organized.

    [:octicons-arrow-right-24: Code review process](../CODE_REVIEW.md)
    &middot;
    [Testing and CI hardening](testing.md)
    &middot;
    [Flake handling](flake-handling.md)

- :material-plug-outline:{ .lg .middle } **Adapter and hook contracts**

    ---

    What a new CLI adapter or lifecycle hook must implement.

    [:octicons-arrow-right-24: Writing adapters](../adapters/ADAPTER_GUIDE.md)
    &middot;
    [Adapter contracts](adapter-contracts.md)
    &middot;
    [Lifecycle hooks contract](hooks.md)

- :material-cog-sync-outline:{ .lg .middle } **CI and release**

    ---

    The apps and checks CI runs, and how a release ships.

    [:octicons-arrow-right-24: CI apps](../maintainers/ci-apps.md)
    &middot;
    [Extended static analysis](../ci/extended-static-analysis.md)
    &middot;
    [Release process](../operations/release.md)

- :material-file-document-edit-outline:{ .lg .middle } **Docs and process**

    ---

    Keeping documentation and automated scans from drifting.

    [:octicons-arrow-right-24: Docs drift playbook](../playbooks/docs-drift.md)
    &middot;
    [Trend scan automation](../devops/trend-scan-automation.md)

</div>
