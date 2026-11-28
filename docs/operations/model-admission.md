# Evidence-qualified model admission

`bernstein model admit` records an explicit operator decision after offline
verification. It never runs a model, selects a replacement provider, or changes
the default model-registry enforcement mode.

The operator chooses a qualified suite, its scoring adapter and the task classes
that suite is suitable to qualify. Those inputs and trusted public keys come from
the operator, independently of the submitted bundle. A bundle's scheduler config,
subject or signer cannot select its own qualification policy. Keep the suite and
public keys in operator-controlled configuration; selecting an arbitrary suite
is itself an admission policy decision.

## Signed subject

New submission bundles use schema version 3. To attribute an evaluation, supply a
JSON subject before signing:

```json
{
  "schema_version": 1,
  "model_ref": {
    "provider": "local",
    "model_requested": "model-a",
    "model_reported": "model-a",
    "version": "revision-1"
  },
  "task_classes": ["code"]
}
```

```bash
bernstein bench run qualified-suite.json --admission-subject subject.json --out bundle.json
bernstein model admit local/model-a@revision-1 \
  --bundle bundle.json --suite qualified-suite.json \
  --task-class code --qualified-task-class code \
  --trusted-key SIGNER_FINGERPRINT=operator-trusted-public.pem \
  --admitted-by operator@example.test --expires-at 2027-01-01T00:00:00.000000Z
```

The example assumes a suite with a production scoring adapter. The built-in
synthetic golden/mock adapter and arbitrary JSON suites without a production
adapter cannot qualify admission. The Python API can inject an operator-trusted
suite-specific replay adapter. `bench run --reliability` emits a different artifact
and therefore refuses `--admission-subject`.

The subject is inside the content hash covered by the existing bundle signature.
It is the signer's explicit model attribution; replay verifies the signed evidence,
not independent observation of a provider's model weights. Operators must trust
the signer's attribution and the suite's measurement suitability. Omitted version
means unpinned identity: evidence pinned to one revision cannot grant admission to
all revisions. A differing reported model identity is refused in this bounded
workflow rather than silently admitted under the requested alias.

## Verification and audit record

Admission requires a real trusted signature, supported bundle/subject versions,
an exact model and snapshot match, requested task classes covered by both the
signed subject and the operator's qualified scope, exact suite identity and task
coverage, and receipt replay `MATCH`. Every replayed task must also pass: `MATCH`
alone can describe faithfully recorded failures or refusals. This workflow refuses
holdout suites until their private tasks can be replayed as admission evidence.
A blanket `*` admission requires an explicit wildcard in both declarations.

No failed qualification appends a model-admission event. Successful records carry
`evidence_ref=sha256:<bundle_hash>` and `verified_evidence` with the subject,
qualified classes, suite hash and signer fingerprint. Admission remains the
named operator's act, with mandatory expiry. Preserve the bundle and suite for
later verification; the event contains their hashes rather than copying them.

For programmatic callers, pass `ModelAdmissionEvidence` as `evidence` to
`record_model_admission` and enable `require_verified_evidence=True`. Supplying
evidence always verifies, even when that flag is omitted. The legacy writer with
only an opaque `evidence_ref` remains available for compatibility; it does **not**
claim evidence qualification. Existing events project exactly as before, and
bundle schemas 1 and 2 keep their original content hashes and signatures. Old
bundles remain readable/verifiable but cannot qualify the new admission workflow.

Exit status is 0 after recording admission, 1 on invalid/unqualified evidence
(and Click's usual 2 for invalid command options).

Verified subjects reject registry delimiters in model identity components and
literal wildcard revisions. Built-in mock replay adapters, including stochastic
fixtures, cannot qualify admission evidence.
