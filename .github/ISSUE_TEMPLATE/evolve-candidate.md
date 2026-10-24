---
name: Evolve Candidate
about: Request that Bernstein implement this feature autonomously
labels: evolve-candidate
title: "[Evolve] "
---

<!--
Bernstein scans issues labelled `evolve-candidate` and `feature-request`.
If your issue passes the trust check (collaborator author or `maintainer-approved`
label), Bernstein will convert it to a backlog task, open a PR, and close
this issue automatically when the PR is merged.

Fill in the sections below so the visionary / analyst pipeline can evaluate
your request accurately. Free-form text is fine - all fields except Problem
are optional.
-->

### Problem

<!-- What is frustrating today? What can't you do? Be concrete. -->

### Proposed solution

<!-- How should it work? CLI examples, pseudocode, or a sketch are all fine. -->

### Impact

<!-- Who benefits, and how much? "Saves N minutes per session", "unblocks X workflow", etc. -->

### Risk

<!-- Any concerns? Breaking changes, security considerations, performance tradeoffs? -->

### Effort

<!-- S / M / L - rough guess. Leave blank if unsure. -->

### Friction receipt (optional)

<!--
If you hit this failure on a real bernstein install you can attach a friction
receipt to get your issue admitted without collaborator status.

A receipt proves you actually ran into the failure: it carries the command,
version, environment fingerprint, and an Ed25519 signature from your install's
key pair.  Generate one with:

    bernstein evolve receipt generate --command "your command" > receipt.json

Then paste the receipt_hash field here and attach receipt.json to the issue.

receipt_hash: <64-char SHA-256 hex>

Receipt-admitted candidates are capped at L0–L2 (never L3) and at
3 per install per day.
-->

**receipt_hash:** <!-- paste here -->
