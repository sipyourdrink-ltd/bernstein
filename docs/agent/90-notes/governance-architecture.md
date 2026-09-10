# Governance Layer Architecture

Bernstein is the open-source governance layer for AI agents.

## Core Architectural Invariants
1. **Deterministic Scheduling**: No model in the coordination loop (`src/bernstein/core/orchestration/orchestrator.py:354`). Workflows execute deterministically across per-task queues.
2. **Per-Task Git Worktrees**: Parallel agent tasks execute in isolated git worktrees created via `src/bernstein/core/agents/spawner_worktree.py:36` so runs replay byte-identically.
3. **Signed Lineage and Offline Audit**: Cryptographic audit chains are managed by `src/bernstein/core/security/audit_chain.py:17` and sealed via `src/bernstein/core/security/audit_receipt.py:118`.
4. **Policy as Code**: Permissions and approvals are enforced deterministically at the orchestration boundary (`src/bernstein/core/orchestration/orchestrator.py:354`).
