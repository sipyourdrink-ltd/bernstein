# Model admission evidence boundary

`eval/bench/admission_subject.py` defines subject schema v1 using the existing
`ModelRef` identity and canonical task classes. `SubmissionBundle` schema v3 hashes
that optional subject before the existing signer seals the bundle hash. Old
bundle payloads omit the extension, retaining their previous hashes.

`eval/bench/model_admission.py` qualifies a snapshot of the submitted bundle
against operator-owned suite/scorer/key/scope inputs. It requires supported
schemas, exact identity and coverage, a trusted real signature and passing
replayed tasks. It performs no inference and consumes no stored MATCH assertion.
The operator must decide the suite's measurement suitability; the signature
attributes the model claim to a trusted signer and does not independently identify
provider weights.

`core/routing/model_registry.py` appends the verified context and content-addressed
evidence reference only after that check returns. Its explicit
`require_verified_evidence` gate refuses missing evidence. Legacy programmatic
opaque-reference writes are preserved and do not claim qualification. The
`model admit` operator CLI always enables the gate; routing defaults and the
historical registry projection remain unchanged.

See [operator workflow](../operations/model-admission.md) and
[subject schema](../api/admission-subject.schema.json).
