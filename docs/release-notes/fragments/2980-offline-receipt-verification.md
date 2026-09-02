## A signed read-set refusal receipt now verifies offline

`verify_receipt_offline` handed an already-constructed `AuditLog` to
`AuditChainStore`, which takes the audit directory and constructs its own. The
call raised `AttributeError` on every input, the function's `except Exception`
guard swallowed it, and the result was `False` — so a correctly signed receipt
anchored in a valid chain was indistinguishable from a forged one, and the
documented offline check could not succeed for any input. The same construction
was used in `git_pr` and `incremental_merge`, where it broke the refusal-receipt
path a read-set mismatch depends on. All three now pass the directory, and the
positive path is covered by a test that anchors a real receipt in a real chain
and verifies it from the on-disk record alone.

`ComparativeBenchmark.run_single_agent` passed `role` and `pid_dir` to
`CLIAdapter.spawn`, which declares neither, and built a `ModelConfig` with a
`provider` field that does not exist. Both raised `TypeError` into a bare
`except`, so every single-agent benchmark reported a failed run rather than an
error. The call now matches the adapter contract.

(#2980)
