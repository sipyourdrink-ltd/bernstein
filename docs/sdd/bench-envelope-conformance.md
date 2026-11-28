# Benchmark-envelope conformance contract

`bernstein.eval.bench.self_conformance_suite` defines the canonical
`self-conformance-bench-v1` tasks and their replay adapter. `bench_cli` selects
both for run and verify, and names the module as the SARIF source location.
The existing runner produces the outer bundle and the existing verifier
replays stored receipts. No new authority or key store is introduced.

Every receipt contains versioned, canonical control and probe bundles.
The adapter compares them to the named case before asking the production
`BenchVerifier` to check both. A pass requires the valid control, the expected
negative headline and the expected per-task results. Receipt fields cannot
assert that a check passed. Offline scoring never calls `run_task`.

Inner fixtures have two tasks with distinct explicit context assertions,
public deterministic Ed25519 material and timestamp zero. Deletion and context
replay are re-signed with that public fixture key to isolate coverage and
replay checks. Other mutations retain the original signature while recomputing
the public bundle hash. These are controlled test inputs, not producer evidence.

The ordinary outer installation signature and independently configured trust
pin establish who produced the measurement. Inner fixture keys establish no
installation authority. This module measures benchmark-envelope behavior;
it does not simulate journal/spine heads or assert full runtime, chain or
release-admission coverage. See the [operator contract](../operations/bench-envelope-conformance.md).
