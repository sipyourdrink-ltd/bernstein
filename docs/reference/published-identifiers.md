# Published identifiers

Bernstein writes absolute URLs under `https://bernstein.run/` into the
documents it signs and exports: DSSE payload types, in-toto predicate types,
receipt `type` fields, schema `$id` values and namespaces. A verifier that
meets one of these URLs can open it and land here.

Published identifiers must resolve. Each one is listed in
[`published-urls.json`](published-urls.json), and the site serves it in one
of three ways:

| Kind | What the URL returns |
|---|---|
| `schema` | The JSON Schema itself, `application/schema+json`, with `$id` equal to the URL |
| `redirect` | A 301 to the section of this page (or the TRACE export page) that defines it |
| `document` | A live document the package fetches, such as a catalog |

A unit test asserts that the set of `https://bernstein.run/` literals in the
source tree equals the manifest, so a new identifier cannot ship without an
entry. The site checks every manifest URL and fails on anything other than
2xx or 3xx.

A `v1` identifier never changes meaning. A wire-format change ships under a
new version path, and both versions stay resolvable.

## Attestation and receipt types

### `attestations/audit/v1` {#attestations-audit-v1}

In-toto predicate type for the DSSE-wrapped audit log export.
Emitted by `bernstein.core.security.audit_dsse`.
See [Audit DSSE envelope](../security/audit-dsse-envelope.md).

### `attestations/audit-receipt/v1` {#attestations-audit-receipt-v1}

Receipt type for a verifiable audit receipt over an audit-chain range.
Emitted by `bernstein.core.security.audit_receipt`.
Schema: [`schemas/audit-receipt-v1.json`](https://bernstein.run/schemas/audit-receipt-v1.json).
See [Verifiable audit receipts](../security/audit-receipt.md).

### `attestations/authority-envelope/v1` {#attestations-authority-envelope-v1}

Envelope type for a signed authority envelope carried across an interop boundary.
Emitted by `bernstein.core.interop.authority_envelope`.
Schema: [`schemas/authority-envelope-v1.json`](https://bernstein.run/schemas/authority-envelope-v1.json).
See [Authority envelope](../interop/authority-envelope.md).

### `attestations/clean-room-verify/v1` {#attestations-clean-room-verify-v1}

Predicate type for a clean-room verification receipt: the task's gates re-run
outside the workspace that produced the result.
Emitted by `bernstein.core.volunteer.clean_room`.

### `attestations/conduct/v1` {#attestations-conduct-v1}

Type of the signed, content-addressed conduct artifact that binds a derived
conduct fold to its source journal.
Emitted by `bernstein.core.replay.conduct_artifact` and `bernstein.core.replay.conduct_fold`.

### `attestations/consent/v1` {#attestations-consent-v1}

Predicate type for a consent receipt: a donor's DSSE-signed consent to run a volunteer task.
Emitted by `bernstein.core.volunteer.consent`.

### `attestations/diagnosis-receipt/v1` {#attestations-diagnosis-receipt-v1}

Receipt type for the signed output of `bernstein audit diagnose`.
Emitted by `bernstein.core.replay.diagnosis_receipt`.
See [Audit diagnose](../operations/audit-diagnose.md).

### `attestations/evidence-envelope/v1` {#attestations-evidence-envelope-v1}

Envelope type for an evidence envelope.
Emitted by `bernstein.core.security.evidence_envelope`.
Schema: [`schemas/evidence-envelope-v1.json`](https://bernstein.run/schemas/evidence-envelope-v1.json).
See [Evidence envelope](../security/evidence-envelope.md).

### `attestations/govern-apply-receipt/v1` {#attestations-govern-apply-receipt-v1}

Receipt type binding the outcome of `bernstein govern apply` to the reviewed plan it executed.
Emitted by `bernstein.core.govern.apply`.

### `attestations/key-succession/v1` {#attestations-key-succession-v1}

Document type for the signed key-succession chain of receipt-signing keys.
Emitted by `bernstein.core.security.receipt_key_chain`.
See [Deterministic replay](../operations/deterministic-replay.md).

### `attestations/result-receipt/v1` {#attestations-result-receipt-v1}

Predicate type for a result receipt bundle, the offline-verifiable unit of a
worker's submission.
Emitted by `bernstein.core.security.result_receipt_bundle`.
See [Volunteer protocol](../volunteer/protocol.md).

### `attestations/run-attestation-receipt/v1` {#attestations-run-attestation-receipt-v1}

Receipt type for identity-bound run evidence.
Emitted by `bernstein.core.security.run_attestation_receipt`.
See [Receipt format](../security/receipt-format-spec.md).

### `attestations/run-receipt/v1` {#attestations-run-receipt-v1}

Receipt type for the signed, offline-verifiable receipt over a whole run.
Emitted by `bernstein.core.replay.run_receipt`.
See [Receipt format](../security/receipt-format-spec.md).

### `attestations/scorecard/v1` {#attestations-scorecard-v1}

Type of the signed, content-addressed scorecard artifact that binds a run
scorecard to its source journal.
Emitted by `bernstein.core.replay.scorecard_artifact` and `bernstein.core.replay.scorecard`.
Scorecard body schema: [`schemas/scorecard/v1`](https://bernstein.run/schemas/scorecard/v1).

### `attestations/trajectory-receipt/v1` {#attestations-trajectory-receipt-v1}

Predicate type for trajectory-receipt projections (COSE, DSSE and
transparency-log forms) of a sealed benchmark trajectory.
Emitted by `bernstein.eval.trajectory_receipt_projection`.
See [Benchmarks](../eval/bench.md).

## Volunteer protocol documents

All volunteer documents are Ed25519-signed DSSE envelopes over canonical
bytes. See [Volunteer protocol](../volunteer/protocol.md).

### `attestations/volunteer/v1` {#attestations-volunteer-v1}

Shared predicate type for volunteer protocol documents. The `document_kind`
field in the predicate body names the kind.
Emitted by `bernstein.core.protocols.volunteer.documents`.

### `attestations/volunteer/claim/v1` {#attestations-volunteer-claim-v1}

A worker has taken responsibility for a task.
Emitted by `bernstein.core.protocols.volunteer.claim`.

### `attestations/volunteer/project_card/v1` {#attestations-volunteer-project-card-v1}

What a project offers to volunteers: task types, gates and limits.
Emitted by `bernstein.core.protocols.volunteer.project_card`.

### `attestations/volunteer/receipt/v1` {#attestations-volunteer-receipt-v1}

A work item was merged and credited to its contributor.
Emitted by `bernstein.core.protocols.volunteer.receipt`.

### `attestations/volunteer/submission/v1` {#attestations-volunteer-submission-v1}

A worker produced a result for a task.
Emitted by `bernstein.core.protocols.volunteer.submission`.

### `attestations/volunteer/verdict/v1` {#attestations-volunteer-verdict-v1}

The outcome of running the gates on a submission.
Emitted by `bernstein.core.protocols.volunteer.verdict`.

### `attestations/volunteer/worker_card/v1` {#attestations-volunteer-worker-card-v1}

What a donor offers to volunteer: capabilities and limits.
Emitted by `bernstein.core.protocols.volunteer.worker_card`.

## Compliance exports

### `compliance/ai-bom/v1` {#compliance-ai-bom-v1}

Schema identifier of the AI Bill of Materials export for a run.
Emitted by `bernstein.core.compliance.ai_bom`.
See [Run BOM](../operations/run-bom.md).

### `compliance/pack-manifest/v1` {#compliance-pack-manifest-v1}

Schema identifier of a compliance evidence pack manifest.
Emitted by `bernstein.core.compliance.pack`.
See [EU AI Act Article 12 bundle](../compliance/eu-ai-act-article-12-bundle.md).

### `oscal` {#oscal}

Namespace (`ns`) of the Bernstein-specific properties in OSCAL
assessment-results exports.
Emitted by `bernstein.compliance.oscal`.

### `spdx/{run_id}` {#spdx-id}

`documentNamespace` of the SPDX encoding of a run's AI-BOM. The trailing
segment is the run id, so each document's namespace is unique. Every
`/spdx/<run_id>` URL redirects here.
Emitted by `bernstein.core.compliance.ai_bom_encoders.spdx`.
See [Run BOM](../operations/run-bom.md).

## TRACE records

`trace/verifier` and `trace/records` redirect to
[Trust records (trace export)](../observability/trace-export.md#trace-verifier).

## Schemas and documents

These are served directly rather than redirected.

| URL | Source in this repository |
|---|---|
| `schema/sdd/ticket.v1.json` | `src/bernstein/sdd/schema/ticket.v1.json` |
| `schemas/*.json` | `schemas/` |
| `schemas/openlineage/1-0-5/BernsteinChainRunFacet.json` | `schemas/openlineage/1-0-5/` |
| `schemas/scorecard/v1` | `src/bernstein/core/replay/scorecard_schema.json` |
| `mcp-catalog.json` | [MCP catalog](mcp-catalog.md) |
| `skills-catalog.json` | [Skills catalog](../operations/skills-catalog.md) |
