# Workflow agent completion boundary proposal

**Status: design review required; no runtime implementation.**

This proposal addresses [#6639](https://github.com/sipyourdrink-ltd/bernstein/issues/6639).
It describes intended behavior, not guarantees provided by the current runner.

## Observed defect

`WorkflowRunner._execute_agent` treats the session returned by
`spawn_for_tasks` as `NodeStatus.SUCCESS`. The dependency resolver therefore
releases consumers before an agent has produced an accepted result. The
node's timeout is not applied to its agent execution.

The version-1 node checkpoint records terminal results only. It has no durable
dispatch intent, task/attempt identity, deadline, or accepted-result evidence.
Waiting in memory alone would still lose the attempt on a crash and permit a
duplicate dispatch on resume.

## Proposed boundary

Keep public terminal node results separate from durable in-flight attempt
state. Introduce a versioned attempt checkpoint owned by the workflow runner;
the persisted representation and upgrade policy require review before code.
Bind each attempt to the manifest digest, run ID, node ID, task ID, iteration,
attempt identity, session identity when known, and the existing tenant and
authorization context. Preserve the node's explicit CLI, model, and effort.

Persist dispatch intent before spawning, then persist its confirmation.
Reconcile an unconfirmed intent with the existing task/session lifecycle;
absence of a confirmation is not permission to spawn again. If that lifecycle
cannot prove whether dispatch happened, retain an unresolved attempt, block
dependents, and report the ambiguity for operator reconciliation.

Read completion from the existing task lifecycle and native verification
pipeline. A session ID, live process, zero exit status, or agent-reported
`DONE` does not alone prove acceptance. Record successful node completion
only after acceptance evidence is bound to this attempt and its output:
the verified code revision or the native artifact acceptance receipt.
Reuse the existing completion and artifact verification authority rather
than building a parallel verifier or minting duplicate receipts on resume.
Reject stale, foreign-run, foreign-task, foreign-attempt, and cross-tenant
evidence without releasing dependencies.

Persist the per-iteration deadline so restart cannot reset the timeout budget.
Route timeout and cancellation through the existing cancellation authority;
an unacknowledged cancellation remains unresolved rather than successful or
safe to retry. Failed, cancelled, timed-out, refused, and unverifiable outcomes
never satisfy dependencies. Evaluate loop predicates and advance iterations
only after acceptance, with no overlap between unresolved attempts.

On resume, validate the manifest and reconcile the same attempt and deadline.
Do not silently trust legacy agent `success` checkpoints lacking acceptance
evidence. The migration policy must specify how to reconcile those records
or require an operator decision. Command-node behavior and intentional
condition skips retain their existing semantics.

## Decisions required before implementation

1. Approve the durable attempt representation, dispatch identity lookup, and
   compatibility policy for existing version-1 checkpoints.
2. Select the authoritative acceptance evidence for code and artifact tasks,
   including its binding to an attempt and authorization context.
3. Define operator reconciliation for unknown dispatch/cancellation outcomes
   without automatic duplicate execution or implicit acceptance.

## Required property test matrix

All lifecycle scenarios must exercise the public DAG with a controlled local
subprocess or task-store-backed fixture and a downstream sentinel side effect.

1. `test_dependencies_wait_for_verified_agent_success`: a running agent and
   an unverified terminal report leave the sentinel absent; accepted success
   releases it exactly once.
2. `test_failed_agent_never_releases_dependents`: failure, refusal, and rejected
   verification leave the sentinel absent.
3. `test_agent_timeout_survives_resume`: timeout retains its original budget,
   requests cancellation, and blocks dependent side effects after restart.
4. `test_cancelled_agent_never_releases_dependents`: cancellation uses the
   native authority; unknown acknowledgement cannot become success or retry.
5. `test_resume_reconciles_running_attempt_without_respawn`: crashes after
   intent and after dispatch confirmation reconcile without duplicate agents.
6. `test_stale_or_foreign_completion_cannot_release_dependents`: old attempts,
   other runs/tasks, and foreign tenant evidence cannot authorize a consumer.
7. `test_artifact_acceptance_is_required_before_dependency_release`: native
   artifact rejection blocks the consumer; accepted evidence releases it.
8. `test_agent_loop_advances_only_after_acceptance`: predicates and the next
   iteration remain blocked while the current attempt is unresolved.
9. `test_legacy_agent_success_requires_reconciliation`: old unproven agent
   checkpoints do not silently release dependents; command resume and
   intentional condition skips retain their established behavior.

## Verification evidence and limits

At baseline `b80e0aa71`, a local pytest reproduction named
`test_dependencies_do_not_run_before_verified_agent_completion` returned
both agent and dependent command as `success` while its controlled subprocess
was still running. The dependent sentinel existed without any terminal
report or acceptance evidence. The reproduction failed as expected.

That reproduction is investigation evidence, not a passing regression shipped
by this proposal. None of the proposed lifecycle properties is implemented
or claimed as verified here.
