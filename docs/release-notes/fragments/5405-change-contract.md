## Typed `ChangeContract` on `UpgradeProposal`

`bernstein.evolution` now re-exports the new `ChangeContract` value type (alongside
`ChangeFalsifier`, `ChangeRollback`, `EffectDirection`, and `PredictedEffect`), and
`UpgradeProposal` carries an optional `contract:` field so evolution proposals
declare their `component`, `target_fingerprint`, `predicted_effect`, `invariants`,
`falsifier`, and `rollback` as a single typed payload. Each sub-type validates on
construction (typed `metric`/`direction`, list-typed `invariants` and
`files_to_restore`, str-typed `component`/`target_fingerprint`/`history_ref`/
`change_description`) and round-trips through `to_dict`/`from_dict`, so proposals
that opt in to a contract carry a falsifiable, rollback-ready specification
alongside the existing `diff` and `expected_impact` fields; proposals without a
contract continue to serialize and load unchanged.

(#5405)
