# ISO/IEC 42001 evidence pack

`bernstein audit export --standard iso-42001` maps a subset of ISO/IEC
42001:2023 Annex A controls onto the same HMAC-chained audit events, lineage
log, and cost ledger that back the `ai-act`, `owasp-asi` and `owasp-skills`
packs. Source: `src/bernstein/compliance/iso42001.py`, registered in
`src/bernstein/compliance/evidence_pack.py`.

This is the first slice of issue #3238: the selectable standard, its control
map, and this page. Offline re-derivation of each `mapped` claim inside
`bernstein audit verify` and the `regulator_renderers.py` extension follow in
a later PR.

## What this is not

Bernstein cannot certify anybody, and an ISO/IEC 42001 certificate attests
that an organisation's management system was audited - not that a tool is
compliant. This pack does not claim conformance. It gives an operator the
per-control evidence their own chain already contains, derived rather than
asserted, so they are not hand-assembling a spreadsheet from raw exports for
every framework they get audited against.

## Three-state honesty rule

Every mapped control resolves to exactly one of three states. No control is
silently absent from the pack.

| Status | Meaning |
|---|---|
| `mapped` | The chain contains records that satisfy the control; the pack cites the concrete artefact and selector. |
| `partial` | The chain covers part of the control; the requirement text states what is missing. |
| `organisational` | The control is about the operator's policy, training, or governance. No tool can evidence it, so it is named explicitly rather than marked `mapped` or dropped from the pack. |

A pack that marked a governance control `mapped` because the code cannot
tell the difference would be worse than no pack: it fails an audit and takes
the operator's credibility with it.

## Coverage

Only controls where a chain record can actually speak to the requirement are
mapped - resource and lifecycle records, impact and event logging,
third-party and data provenance, and human oversight of individual
decisions. Full Annex A has roughly forty controls across ten themes; this
slice covers the records-derivable subset. See `DEFERRED` in
`iso42001.py` for the themes intentionally left out of this slice, and why.

| Control | Requirement (short) | Artefact | Status |
|---|---|---|---|
| `A.6.2.8` | AI system event logging | `audit-chain/events.jsonl` | `mapped` |
| `A.6.2.6` | AI system operation and monitoring | `audit-chain/events.jsonl` | `mapped` |
| `A.7.5` | Data provenance | `lineage/log.jsonl` | `mapped` |
| `A.4.5` | System and computing resources | `costs/cost_history.jsonl` | `mapped` |
| `A.10.3` | Suppliers (third-party component risk) | `audit-chain/events.jsonl` | `partial` |
| `A.4.3` | Data resources | `audit-chain/data_catalog.json` | `partial` |
| `A.4.4` | Tooling resources | `audit-chain/events.jsonl` | `partial` |
| `A.8.4` | Communication of incidents | `audit-chain/events.jsonl` | `partial` |
| `A.9.2` | Processes for responsible use | `audit-chain/events.jsonl` | `partial` |
| `A.6.2.4` | AI system verification and validation | `audit-chain/events.jsonl` | `partial` |
| `A.5.2` | AI system impact assessment process | n/a | `organisational` |
| `A.2.2` | AI policy | n/a | `organisational` |
| `A.3.2` | AI roles and responsibilities | n/a | `organisational` |
| `A.10.2` | Allocating responsibilities (provider/customer) | n/a | `organisational` |

4 `mapped`, 6 `partial`, 4 `organisational` - 14 of 14 controls counted, none
silently dropped from the pack summary. The full requirement text (including
what a `partial` control is missing) lives in `iso42001.py`; this table is
the at-a-glance view, not the source of truth.

This document does not duplicate the ISO/IEC 42001 clause text, which is
licensed; each row links a control id to the bernstein mechanism, not to the
standard's wording.

## Cross-walk to the existing FINOS AIGF mapping

`docs/compliance/finos-aigf-mapping.md` already cross-walks ISO/IEC 42001
clause 7.5.3 ("control of documented information") and clause 9
("performance evaluation") - main-body management-system clauses, not Annex
A - onto `CTRL-AUDIT-TRAIL`, `CTRL-RETENTION` and `CTRL-INCIDENT-RESPONSE`.
The Annex A rows above use the same underlying mechanisms for the
overlapping ground (event logging, retention, incident recording), so the
two documents describe the same evidence rather than disagreeing about what
is covered.

## Building a pack

```
bernstein audit export --standard iso-42001 --out iso42001-evidence.zip
```

Produces the same deterministic zip layout as the other standards
(`manifest.json`, `controls.json`, `audit-chain/`, `lineage/`, `costs/`,
`README.md`) with `manifest.json` carrying
`controls_mapped` / `controls_partial` / `controls_organisational` counts
that always sum to the number of controls in the map.
