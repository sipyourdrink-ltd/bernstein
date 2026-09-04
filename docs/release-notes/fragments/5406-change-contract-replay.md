## Replay service for ChangeContract (Issue #5406)

The evolution engine now ships a `replay_contract` service that re-executes a
recorded `ChangeContract` against a thin corpus and produces a deterministic
`RunVerdict`. The verdict is written to a canonical receipt whose fingerprint
is bound to the contract's canonical bytes; `verify_verdict_receipt` re-checks
that binding before declaring the replay match, naming the diverging field on
mismatch. Public additions: `change_contract_replay` module
(`replay_contract`, `select_corpus`, `write_verdict_receipt`,
`verify_verdict_receipt`, `contract_canonical_bytes`, `contract_fingerprint`,
`register_invariant`) and types `ChangeContract`, `PredictedDecisionChange`,
`ContractInvariant`, `RunVerdict`, `ReplayServiceResult`, `ReplayVerdict`.
(`#5406`)
