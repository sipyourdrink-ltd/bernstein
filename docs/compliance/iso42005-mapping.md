# ISO/IEC 42005:2025 evidence boundaries

ISO/IEC 42005:2025 provides guidance for performing an impact assessment.
The pack supplies evidence for the operator's own assessment and makes
no compliance or certification claim.

## Exporting evidence

`bernstein audit export --standard iso-42005` exports a deterministic projection
of the same filtered chain window as `iso-42001`. Use identical `--since` and
`--task` values to compare the shared audit, lineage and cost members: their
bytes are unchanged. The assessment mapping, README and manifest differ.
Capability still differs from evidence present in an export window.
See the [ISO/IEC 42001 mapping](iso42001-mapping.md) for the companion pack.

## Coverage

These five `control_id` values are Bernstein identifiers, not ISO clause numbers.
Verdicts describe capability, not completed assessments. Mechanisms inform
the operator's work; their existence alone does not establish assessment content.

| Element | `control_id` | Bernstein mechanism | Verdict | Remaining gaps |
|---|---|---|---|---|
| Intended use | `impact.intended-use` | The compliance preset configures governance and audit capture. | Not-covered | Operator-owned purpose, deployment context and use boundaries must be documented; configuration does not state them. |
| Affected parties | `impact.affected-parties` | Actor/task attribution and lineage receipts trace recorded activity. | Not-covered | Operator-owned stakeholder identification, affected groups and consultation are absent from provenance alone. |
| Foreseeable misuse | `impact.foreseeable-misuse` | The guardrail inventory identifies protections; the incident pack collects recorded failures for investigation. | Partial | Operator analysis must identify foreseeable misuse scenarios and impacts beyond observed incidents and listed protections. |
| Mitigations | `impact.mitigations` | The compliance preset, guardrail inventory and lineage receipts inform configured protections and traceability. | Partial | Operator validation must establish effectiveness, residual impacts and links to assessed harms; configuration and hashes alone are insufficient. |
| Review cadence | `impact.review-cadence` | The incident pack and timestamped audit/lineage records inform review triggers. | Not-covered | Operator-owned review schedule, accountable owners and reassessment decisions must be supplied; timestamps establish no cadence. |

## D1: catalogue contract

- Covered → `mapped`; Partial → `partial`.
- Not-covered → `organisational` for explicitly operator-owned content/processes, otherwise `todo`.
- State gaps in `requirement`. For organisational rows, both `artefact` and `selector` are `"n/a"`.
- An unclassified known element defaults to Not-covered/`todo`; no element defaults to Covered.
- Missing, duplicate or unknown IDs and invalid verdicts/statuses raise `ValueError`.
- These decisions do not preapprove any individual Covered verdict.

## D2: subsequent exporter contract

This is the contract for the subsequent exporter implementation, not behaviour delivered by this documentation change.

- For `iso-42005` only, each exported control receives `evidence_refs: list[str]`, default `[]`:
  sorted, deduplicated `<member>:<positive-1-based-line>` strings referencing
  final serialized `audit-chain/events.jsonl` or `lineage/log.jsonl` members.
- Catalogue verdicts describe capability ceilings. Project onto copied controls using
  private, element-specific predicates over the existing filtered records. Emit the catalogue's
  `mapped` or `partial` status only when its predicate matches and references resolve;
  otherwise emit `todo`. Retain `organisational` and recompute summary counters from exported statuses.
- Unknown matching rules or invalid, dangling or nonmatching references raise `ValueError` before writing.
  Unrelated records, generic policy files, inventories and provenance hashes alone
  cannot establish assessment content, including intended use or affected parties.
- An empty window remains valid, with empty references and zero mapped/partial controls.
  Resolving references establishes evidence presence; chain authentication remains a separate verification procedure.
- Existing standards, API signatures, filters, source collection, shared evidence-member bytes and
  schema version `1.0.0` remain unchanged. No migration or additional authority is introduced.

## Standards context

The public [ISO/IEC 42005:2025 overview](https://www.iso.org/standard/42005)
describes its guidance role. [ISO/IEC 42006:2025](iso42001-mapping.md#role-of-isoiec-420062025)
addresses audit and certification bodies. This page does not reproduce licensed standard text.
