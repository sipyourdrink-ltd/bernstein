# Reported-activity receipt coverage: remaining contract

Status: proposal for #4968; requires a receipt-schema and compatibility decision.
No runtime behavior changes in this proposal.

[ADR-017](../decisions/017-receipt-states-its-gap.md) already requires receipts
covering foreign activity to identify the reporting adapter and version, name
its declared unobservable surfaces, and reproduce that statement offline.
The merged implementation supplies part of this contract.

## Current behavior and root cause

`CoverageSource` in `src/bernstein/core/security/run_closure.py` records the
profile, source kind, coverage code, detail and count, without adapter version.
`derive_coverage_statement` groups by profile and source kind and retains the
first declaration. A later declaration with a different known gap increments
the count without retaining that gap or refusing issuance.

`build_run_attestation_receipt` signs this projection. The offline semantic
verifier reuses the same derivation, so it accepts the same information loss.
Empty coverage detail already prevents issuance; that check does not cover
conflicting nonempty declarations.

## Proposed boundary and decisions

Carry the actual ingest adapter declaration identity and version through the
authenticated event evidence into the receipt's coverage statement. The
version must describe the adapter that reported the activity; an arbitrary
version supplied by an untrusted span is insufficient. Preserve the distinction
between scheduler-executed events and reported events and preserve each
adapter's declaration rather than inferring missing observations.

Before implementation, decide:

1. **Schema and identity:** how versioned adapter identity binds to each reported
   portion, including adapters that change version during a retained range.
2. **Declaration changes:** whether distinct declarations for the same identity
   become separate portions or cause issuance to refuse with an explicit
   conflict. In either case, later known gaps cannot silently disappear.
3. **Compatibility:** how new receipts represent or refuse historical evidence
   lacking version information, and how the verifier handles existing v1
   receipts. Preserve verification of original signed bytes; do not rewrite
   historical claims or manufacture an adapter version or confidence level.

Keep the existing signature scheme and offline verification boundary. A valid
signature authenticates the retained statement; it cannot establish that a
foreign runtime reported every event or prove what its adapter could not
observe. Coverage names that limitation without deciding whether a coverage
level is acceptable. No network lookup or access to the original run may be
required to reproduce a receipt's stated coverage.

## Numbered test matrix

1. `test_mixed_run_separates_executed_and_reported_portions`: preserve the split
   and exact event totals.
2. `test_reporting_adapter_declaration_version_survives_signed_receipt`: bind
   the actual declaration version rather than caller-controlled span metadata.
3. `test_distinct_adapter_versions_keep_separate_coverage_portions`: an adapter
   upgrade cannot silently collapse identities.
4. `test_every_declared_unobservable_surface_is_named`: retain every known gap,
   or refuse issuance under the approved declaration-conflict rule.
5. `test_coverage_declaration_conflicts_never_silently_take_first`: exercise
   changes in both coverage code and detail, including reversed arrival order.
6. `test_missing_known_coverage_detail_blocks_receipt_issuance`: retain the
   existing fail-closed behavior for empty declarations.
7. `test_legacy_unversioned_evidence_uses_explicit_compatibility_state`: exercise
   the approved migration or refusal rule without invented identity facts.
8. `test_offline_verifier_reproduces_all_versioned_coverage_without_run_or_network`:
   verify serialized receipt bytes after deleting original audit files.
9. `test_offline_verifier_rejects_omitted_or_replaced_coverage_portion`: compare
   the complete projection with its retained authenticated evidence.
10. `test_existing_v1_receipt_signature_verification_preserves_original_bytes`:
    preserve historical signature verification while stating its semantic limits.

## Current-main reproduction

At `b80e0aa71ee7fc444165b407b8bd0fe3d3051248`, both existing security test
files pass: 25 receipt tests and 21 run-closure tests. Temporary adversarial
checks fail for omitted adapter versions and for a second distinct known gap
under the same profile/source-kind pair. A control removes the original audit
directory and confirms the offline semantic verifier still accepts that lossy
projection. These are two failing properties and one passing defect control,
not a completed implementation or green regression tests shipped by this PR.
