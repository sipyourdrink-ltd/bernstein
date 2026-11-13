# Workflow agent completion: contract review (candidate)

**Status: DESIGN_REVIEW_CANDIDATE. This is not an approval. Runtime work is NOT_RELEASED.**

This document reviews the design-only proposal for
[#6639](https://github.com/sipyourdrink-ltd/bernstein/issues/6639)
([PR 6696](https://github.com/sipyourdrink-ltd/bernstein/pull/6696),
`docs/architecture/workflow-agent-completion-proposal.md`, merged as `8ab0972dfa08`). It
reuses that draft's boundary and its numbered property matrix and does not
replace them. It changes no runtime code. The executable evidence is in
`tests/integration/test_workflow_verified_completion_contract.py`.

## 1. Current-base result

- Inspected base: `cde32dad`. `src/bernstein/core/workflows/`,
  `src/bernstein/core/tasks/` and `tests/integration/test_workflow_runner.py`
  are byte-identical at `ed978765`, the tip of `main` when this review ran.
- **The defect remains.** `WorkflowRunner._execute_agent` returns
  `NodeStatus.SUCCESS` as soon as `spawn_for_tasks` returns a session
  (`src/bernstein/core/workflows/workflow_runner.py:1020-1024`).
  `node.timeout_seconds` applies to command nodes only (`:943`).
- `WorkflowRunner` holds no `TaskStore` (`:360-373`), and the `wf-*` task it
  builds (`:999-1008`) is never registered in a store. No native completion
  pipeline can see that task, so nothing can accept it.
- The harness reproduces the defect against the real `WorkflowRunner`, a real
  `TaskStore` and controlled child processes. 18 proposed-contract tests fail,
  and every failure is on the property itself, not on a harness precondition.
  See section 5.

### Post-merge revalidation

After merging `origin/main` at `85a8ac239e71`, the contract suite still reports
**18 failed, 8 passed** on macOS with Python 3.14.3. All 18 failures remain on
the proposed properties; the acceptance interface is still absent. No runtime
contract is implemented by this PR, and these assertions remain unchanged.

The harness now falls back to a signal-0 PID probe when procfs is unavailable.
Previously, a missing `/proc/<pid>/stat` on macOS incorrectly meant "exited",
which could omit a surviving child of a killed runner from cleanup. The
existing harness soundness test now asserts that a live child is detected
before verifying that it is reaped. That assertion failed before the fix and
passes afterward.

The related integration selection (`test_workflow_runner.py`,
`test_abandon_lifecycle.py`, `test_adapter_conformance_lifecycle.py`) passes
42 tests. Main now includes the abort regression and fix discussed in
section 7; the original base observations below are historical.

### Revalidation on main 11481be64e5e

Including the existing runner tests, the Python 3.13 baseline reports
**22 failed, 36 passed**. One additional failure directly tests proposed rule 4: a runner with a working
spawner and no completion authority must fail before dispatch. Current main
calls the spawner once. The test observes dispatch and dependent execution.
It does not prescribe which run bookkeeping can be written before refusal.
Another failure shows that current main releases the consumer after native
artifact verification and a same-task, same-session CLOSED transition without
attempt-bound acceptance. It leaves the accepted-artifact positive control open.
The loop test now also covers an agent that exited successfully and reported
`DONE` without acceptance. The runner evaluates the predicate and dispatches
three iterations in both the live and reported-done cases.
An unacknowledged cancellation leaves the controlled child alive. Reopening
the task journal restores `STOP_REQ`, its version and its stop summary.
The original claim session is absent after replay. The runner writes
`run_complete.json`, so the resume test fails on that marker before it can
verify reconciliation without redispatch.

The crash cases now prove a live child and no terminal checkpoint before
restart. The second hook runs before the terminal checkpoint. Coverage
after durable dispatch acknowledgement awaits an implementation seam.
The v1 resume tests use literal v1 records, so changes to the current
writer cannot silently update the compatibility fixtures.

## 2. Native interfaces (verified at base)

| Need | Existing native carrier | Gap |
|---|---|---|
| Crash-safe runner state | `write_atomic_json` (`core/persistence/atomic_write.py`): temp file, fsync, replace, directory fsync. It already writes `spec_snapshot.json` and `<node>.node.json`. | Checkpoints are terminal only (`STATE_VERSION = 1`, `workflow_runner.py:176`). `load_node_state` never reads `version` (`:273`). |
| Session and process identity | `AgentSession` (`core/tasks/models.py:1202`): `id`, `pid`, `task_ids`, `spawn_ts`. The id is minted inside `spawn_for_tasks` (`core/agents/spawner_core.py:4653`). | A caller cannot supply the id beforehand. No durable task-to-session lookup exists on the spawner. |
| Dispatch evidence after a restart | `TaskStore` status replay. After a claim, `CLAIMED` survives a restart. | Replay restores task status and CAS version from later records (`core/tasks/task_store_core.py:836-849`). The claim session and closure timestamp remain unset (pinned by `test_native_task_store_replay_does_not_preserve_attempt_identity`). Server boot also reopens stale claims (`core/server/server_app.py:1190`). |
| Idempotent registration | None | `TaskStore.create` overwrites a record with the same id, so a CLOSED task becomes OPEN (`task_store_core.py:1631`). Look up before creating. |
| Deadline | `Task.deadline` (`models.py:578`) | `_task_to_record` never persists it. No persisted absolute deadline exists anywhere. |
| Cancellation and acknowledgement | `reap_process_group` returns `ProcessReapReceipt.confirmed_dead` (`core/config/platform_compat.py:347,691`). The cooperative path is `TaskStore.cancel_cascade(..., cooperative=True)` (STOP_REQ) followed by `confirm_stop` (CANCELLED) (`task_store_core.py:3338,3432`). | `AgentSpawner.kill` returns `None` (`spawner_core.py:7370`). The cooperative acknowledgement path needs the orchestrator tick (`core/agents/stop_reaper.py`). |
| Accepted vs. reported | DONE means the agent reported done. CLOSED means janitor pass, merge and close (`core/tasks/task_lifecycle.py:4008-4009`). | `close(task_id)` takes no evidence (`task_store_core.py:2450`). A task with no completion signals is "marked verified by default" (`task_lifecycle.py:5036-5045`). `_dependencies_satisfied` counts DONE (`:1471-1482`). |
| Artifact hash authority | `record_artifact`, `load_receipt` and `verify_artifact` (`core/lineage/artifact_record.py:139,225,233`). They re-derive the content hash and check the signed lineage entry. | The receipt binds `task_id` and the hash only. It has no attempt, session, run or tenant, and the file is keyed by task id, so it survives a reopen (pinned by `test_native_artifact_receipt_binds_task_and_hash_only`). |
| Attempt-bound acceptance read | **ABSENT** | No API answers "this attempt's output, with this digest, was accepted for this tenant" (pinned by `test_gate_accepted_attempt_positive_control_has_no_native_interface`). |

## 3. Recommended contract: workflow agent attempt v2

This is one contract, versioned as `workflow.agent_attempt/2`.

1. **Attempt record.** The runner owns a sidecar file at
   `<run_state_dir>/<node_id>.iter<n>.attempt.json`, written with
   `write_atomic_json`.
   - Fields: `schema`, `run_id`, `spec_digest`, `node_id`, `iteration`,
     `attempt`, `task_id`, `tenant_id`, `session_id`, `pid`/`pgid`,
     `intent_at_epoch`, `deadline_epoch` (wall clock, `intent + timeout_seconds`),
     `state`, `outcome`, `acceptance{authority, ref, output_digest}`,
     `cancel{requested_at, ack}`.
   - States: `intent`, then `dispatched`, then either `resolved` or
     `unresolved`. The `resolved` outcomes are `accepted`, `failed`,
     `refused`, `rejected`, `cancelled` and `timed_out`. The `unresolved`
     outcomes are `dispatch_unknown` and `cancel_unacknowledged`.
   - `<node>.node.json` stays terminal-only. It moves to version 2 and is
     written only after the attempt resolves.
2. **Dispatch.** Look up the task before creating it. Persist `intent` before
   `spawn_for_tasks`, and persist `dispatched` (session id, pid) as soon as it
   returns. On resume:
   - No record means the attempt is known not started, so dispatch it.
   - `intent` without `dispatched` means `dispatch_unknown`: block it and do
     not respawn.
   - `dispatched` means reattach to the same attempt with the same deadline.
3. **Acceptance.** Dependencies are released, and a loop predicate runs, only
   on a native acceptance record that matches the attempt's `task_id`,
   `tenant_id`, `session_id`/`attempt` and `output_digest`. The digest is the
   merge commit for code, or the artifact `entry_hash` re-verified by
   `verify_artifact`.
   - The runner never mints acceptance.
   - Exit 0, `DONE`, a session id, CLOSED or a receipt are not acceptance on
     their own.
4. **No authority configured.** Agent nodes fail closed before dispatch, with
   no side effect.
5. **Deadline and cancellation.** The deadline comes from the attempt record.
   When it expires, the runner requests cancellation through the existing reap
   path, or through `cancel_cascade(cooperative=True)` when the task is
   store-backed.
   - Acknowledgement means `confirmed_dead` or CANCELLED. With neither, the
     state is `cancel_unacknowledged`.
   - A cancelled attempt is never a success and is never retried
     automatically.
6. **Unresolved attempts.**
   - The node is reported as `FAILED` with the error prefix
     `attempt_unresolved:`, and its dependents stay blocked.
   - `run_complete.json` is not written, so the run stays resumable.
   - The runner emits the audit event `workflow.attempt_unresolved`.
7. **Loops.** A loop never has two unresolved attempts at once. The predicate
   is evaluated only after acceptance.
8. **Legacy checkpoints.** A version 1 `success` on an agent-kind node with a
   non-empty `session_id` is `legacy_unproven`. Its dependents stay blocked,
   it is not respawned, and an operator decides. Version 1 command checkpoints
   and condition skips keep their current behavior.

**Rejected alternatives.**

| Alternative | Reason for rejection |
|---|---|
| In-memory wait | It is lost on a crash, and the agent is respawned on resume. |
| `WALWriter` under `.sdd/runtime/wal/` | Orchestrator startup scans, recovers and closes other runs' WALs (`core/orchestration/orchestrator.py:3773-3885`). |
| `EventJournal.record` | It drops `OSError` silently. |
| Re-deriving the attempt from TaskStore replay | Replay restores the CAS version and loses the claim session and closure time. |
| CLOSED or DONE as acceptance | Neither carries an attempt or output binding, and CLOSED can be reached without verification. |
| Artifact receipt alone | It has no attempt or tenant, and it survives a reopen. |
| A runner-side verifier or receipt | A second authority. The draft forbids it. |
| Respawning on an unknown outcome | Duplicate side effects. |
| Silently upgrading version 1 success | It trusts unproven results. |

**Compatibility and rollback.**
- The sidecar is additive. Older binaries ignore `.attempt.json`, so before
  rolling back an operator must drain or resolve every in-flight attempt.
  Otherwise the older binary sees no node checkpoint and respawns.
- Run-state paths do not change (`<workdir>/runs/<run_id>/`).

## 4. Decisions

| # | Open question | Recommended choice | Evidence | Remaining approver |
|---|---|---|---|---|
| D1 | Durable attempt representation, dispatch lookup, version 1 compatibility | Runner-owned `.attempt.json` v2 (contract items 1, 2 and 8) | Replay loss and duplicate overwrite (section 2); P5 and P9 RED; simulation GREEN | `src/bernstein/core/` code owner plus quorum (`.github/CODEOWNERS`, `docs/governance/review-charter.md`) |
| D2 | Authoritative attempt-bound acceptance | A new native `AcceptanceRecord`, written on the existing close path (`task_lifecycle.py:4009`) and restored by replay, plus a read API such as `TaskStore.get_acceptance(task_id) -> AcceptanceRecord \| None`. The interface is **absent**, so this stays blocked. | GATE test; receipt and CLOSED gaps (section 2) | Maintainer. This is an evidence and security boundary. |
| D3 | Unknown dispatch or cancellation outcome | `unresolved` attempt state: block, keep the run resumable, never respawn. The operator verb for abandoning an attempt is still open. | P4 and P5 RED; simulation GREEN | `core/` code owner plus quorum. The CLI verb also needs docs review. |
| D4 | Behavior with no authority configured | Fail closed before dispatch (contract item 4) | Three existing tests assert that dispatch means success (section 6) | `core/` code owner plus quorum |
| D5 | Deadline persistence | Absolute `deadline_epoch` in the attempt record. A clock seam for tests is optional. | P3 RED; simulation GREEN | Same as D1 |

## 5. Property matrix evidence

Column key:
- **Base**: result on the inspected base.
- **Sim**: result under an uncommitted scratch monkeypatch of contract items 1
  to 8. It was used only to show that each expectation can be met. It is not
  an implementation.

| # | Test (`tests/integration/test_workflow_verified_completion_contract.py`) | Kind | Base | Sim |
|---|---|---|---|---|
| 1 | `test_live_claimed_attempt_does_not_release_dependents` | proposed | RED: consumer ran while the child was alive and CLAIMED | GREEN |
| 1 | `test_exit_zero_and_reported_done_without_acceptance_does_not_release` | proposed | RED: exit 0 plus DONE (`closed_at` unset) released the consumer | GREEN |
| 1/7 | `test_gate_accepted_attempt_positive_control_has_no_native_interface` | gate | GREEN. The positive control is **UNEXERCISABLE**: no acceptance interface exists. | GREEN |
| 2 | `test_failed_refused_or_rejected_attempt_never_releases_dependents[failed,refused,verification_rejected]` | proposed | RED ×3: the node reported success after FAILED, REFUSED, and reopen (janitor reject) | GREEN |
| 2 | `test_spawn_exception_fails_node_and_blocks_dependents` | existing | GREEN | GREEN |
| 2 (rule 4) | `test_missing_completion_authority_blocks_before_dispatch` | proposed | RED on current main: the spawner was called once without a completion authority | Not run in the historical simulation |
| 3 | `test_agent_deadline_survives_runner_restart` | proposed | RED: phase 1 released the consumer and never timed out | GREEN |
| 4 | `test_cancelled_attempt_never_releases_or_retries[acknowledged,unacknowledged]` | proposed | RED ×2: CANCELLED (`confirmed_dead`) and STOP_REQ (child alive) both reported success | GREEN |
| 4 | `test_pending_cancellation_keeps_run_resumable` | proposed | RED on current main: live child and STOP_REQ coexist with a run-complete marker | Not run in the historical simulation |
| 5 | `test_resume_after_crash_before_dispatch_dispatches_once` | proposed, already met | GREEN: known not started, dispatched once | GREEN |
| 5 | `test_resume_after_crash_around_dispatch_does_not_respawn[lost_dispatch_ack,before_terminal_checkpoint]` | proposed | RED ×2: spawn counter 2 for the same task id. The latter hook fires before the terminal checkpoint and does not establish durable dispatch acknowledgement. | GREEN |
| 6 | `test_stale_or_foreign_evidence_cannot_release_dependents[5 kinds]` | proposed | RED ×5: other run, other node, foreign tenant, stale attempt, tampered receipt | GREEN |
| 7 | `test_native_artifact_rejection_blocks_dependents` | proposed | RED: `verify_artifact` rejected the artifact and the consumer still ran | GREEN |
| 7 | `test_verified_artifact_and_closed_task_without_attempt_acceptance_block_dependents` | proposed | RED on current main: native verification passed and the current task closed without attempt-bound acceptance. The consumer ran once. | Not run in the historical simulation |
| 8 | `test_agent_loop_waits_for_the_current_attempt[live,reported-done]` | proposed | RED ×2: 3 predicate runs and 3 dispatches, both with live children and with clean exit plus unaccepted `DONE` | GREEN for live case only |
| 9 | `test_legacy_unproven_agent_success_checkpoint_is_not_trusted` | proposed | RED: a version 1 agent success released the consumer (no respawn) | GREEN |
| 9 | `test_legacy_command_checkpoint_resume_is_unchanged`, `test_legacy_condition_skip_checkpoint_resume_is_unchanged` | existing | GREEN | GREEN |
| - | `test_harness_sentinel_dispatcher_and_reaper_are_sound`, two `test_native_*` facts | existing / native fact | GREEN | GREEN |

Totals:
- Base: 18 failed, 8 passed (about 4 s, repeated 3 times with the same result).
- Simulation: 26 passed.
- Existing `tests/integration/test_workflow_runner.py`: 22 passed on the base.
- No controlled child survived any run.

## 6. Implementation follow-on (NOT_RELEASED)

**Stage 0 (landed on the merged main).** See section 7, first finding.
- Files: `workflow_runner.py`, `tests/integration/test_workflow_runner.py`,
  and a release-notes fragment.

**Stage 1 (needs D4).** Fail closed without an authority, and distrust
legacy version 1 agent success.
- Files: `workflow_runner.py`, `tests/integration/test_workflow_runner.py`,
  `docs/operations/workflows.md`.
- Three existing tests encode "a session id means success" and must change in
  the same PR: `test_fresh_context_uses_distinct_task_ids_per_iteration`,
  `test_agent_node_forwards_routing_hints_to_task` and
  `test_agent_node_dispatches_through_real_spawner`. Under the simulation they
  were the only existing tests that changed.

**Stage 2 (needs D1, D3 and D5).** Attempt sidecar, persisted deadline,
reap-receipt cancellation, and resume reconciliation.
- Files: `workflow_runner.py`, the `_contract_runner` seam in the harness,
  `docs/operations/workflows.md`.

**Stage 3a (needs D2).** Native `AcceptanceRecord`, written on the close path
and restored by replay.
- Files: `task_store_core.py`, `task_lifecycle.py`, and one unit test.
- Coordinate with open PR 6838, which touches the same completion and close
  region. Do not overwrite it.

**Stage 3b.** The runner consumes the acceptance record. In the harness, the
GATE test is replaced by the positive control: an accepted attempt releases
the sentinel exactly once.
- Files: `workflow_runner.py`, the harness, `docs/operations/workflows.md`.

**Regression gate for every stage:**

```bash
uv run python -m pytest -q tests/integration/test_workflow_runner.py tests/integration/test_workflow_verified_completion_contract.py
```

**Migration and rollback.**
- Stages 1 and 2 roll back cleanly only after in-flight attempts are resolved
  (section 3).
- Stage 3a adds journal fields, so older replays ignore them.

**Hard requirement.** No stage may introduce acceptance that is not produced
by the native close/verification path and bound to the attempt.

## 7. Incidental findings outside this contract

- **`run()` keeps dispatching after an abort.** The `continue` at
  `workflow_runner.py:634` sits inside the inner `for node in layer` loop
  (in `resume()`, at `:470`, it does not).
  - Reproduction: in the DAG `a: exit 1`, `b: true`, `c` depends on `b`, the
    run returns `[(a, failed), (b, success), (c, skipped), (c, success)]` and
    `c` writes its side effect.
  - Resolved on the merged main; the current
    `test_run_dispatches_nothing_in_layers_after_an_abort` passes.
- **`run()` with an existing `run_id` never compares digests.** It rewrites
  the snapshot without the digest check its docstring promises (`:592-593`,
  `:617`).
- **Run state lands in `<workdir>/runs/<run_id>/`, not in `.sdd/runs/`.**
  The docstrings describe `.sdd/runs/` (`:191-203`). The runner accepts run ids
  of 128 characters, but the journal accepts 64 (`core/replay/journal.py:117`).
- **The CLI never passes a spawner** (`cli/commands/workflow_cmd.py:500,589`).
  Agent nodes therefore fail when run from the CLI.
- **The existing suite writes outside its temp directory.** It writes
  `.sdd/runtime/decisions.jsonl` into the current working directory, through
  the real-spawner test. A bare `TaskStore` archives to the cwd-relative
  `.sdd/archive/` unless `archive_path` is given. The new harness pins both
  paths under `tmp_path`.

## 8. Reproduction and limits

```bash
uv run python -m pytest -q -p no:randomly tests/integration/test_workflow_verified_completion_contract.py
```

- On the revalidated main above, the harness is expected to report 19
  failed and 8 passed. The earlier base and simulation results are historical.
- It is POSIX-only: it uses process groups, with procfs liveness where
  available and a signal-0 PID probe otherwise.
- It runs on Python 3.12 or later. This review used 3.12.3.
- It is offline and synthetic. No provider, model, credential, remote executor
  or production state is involved.
- `TaskStore` is used single-process, as its docstring requires. Restarts are
  sequential handoffs, never concurrent writers.
- The harness dispatcher imitates the shape of `AgentSpawner.spawn_for_tasks`
  and adds the TaskStore registration that an orchestrator performs. It is not
  the production spawner.
