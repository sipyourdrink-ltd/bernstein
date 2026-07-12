---
name: bernstein-run
description: >
  Run a verified multi-agent goal with Bernstein. Use when a task is too
  large for a single agent session: Bernstein decomposes the goal into
  tasks, spawns CLI coding agents in parallel git worktrees, verifies
  their output, and merges results. Also use to check run status, costs,
  and to verify a finished run against its lineage and audit chain.
---

# Run a verified multi-agent goal

Bernstein is a deterministic multi-agent orchestrator for CLI coding
agents. This skill wraps the three-step operator loop: submit a goal,
monitor progress, and verify the finished run against its Merkle-chained
lineage journal and HMAC audit chain.

## Prerequisites

- `bernstein` on PATH (`pip install bernstein` or `uv tool install bernstein`).
- Run commands from the project root (the directory holding `.sdd/` after
  the first run).
- First-time setup: `bernstein init` scaffolds config and templates.

## Workflow

### 1. Submit

```bash
bernstein run -g "<goal>"
```

- The goal is a plain-English description of what to build or fix.
- Bernstein plans tasks, assigns roles (backend, frontend, qa, security,
  devops), and spawns agents in isolated git worktrees.
- Preview without spending: `bernstein run --dry-run -g "<goal>"`.

### 2. Monitor

```bash
bernstein status
bernstein cost
```

- `status` shows task summary and agent health.
- `cost` shows the spend so far against configured budgets.

### 3. Verify

```bash
bernstein lineage verify <run_id>
bernstein audit verify
```

- `lineage verify` recomputes the run's Merkle hash chain and every HMAC
  tag; exit 0 means the chain is intact and non-empty.
- `audit verify` checks the HMAC audit chain and Merkle tree across the
  whole workspace; run it before trusting or shipping the result.

Report the run as complete only when both verifications exit 0.

## Verifying this skill's own install

Installs of this skill are receipt-backed. To confirm `bernstein` is on PATH
and the bundled skill asset resolves in this session:

```bash
bernstein skills package show
```

To prove the skill content the session is driving matches what was
installed:

```bash
bernstein skills package verify --dest <path-to-this-skill-directory>
```

Exit 0 means the installed tree's content address matches a receipt
anchored in the lineage spine and audit chain. These two commands are the
self-check contract the multi-host conformance sweep replays per agent host
(`bernstein skills package conformance`).

## Notes

- Never mark a goal done from agent output alone; the lineage and audit
  verifications are the completion evidence.
- If `bernstein` is not installed, say so and point at the prerequisites
  above; do not attempt to reimplement orchestration inline.
- See `references/worked-examples.md` for end-to-end transcripts and
  per-host invocation notes.
