# Engagement Playbook Templates

Structured blueprints for security engagements that run multiple scanner phases, each gated by an `EngagementMandate` scope grant.

## Schema

```yaml
id: <string>                    # unique playbook identifier
name: <string>                  # human-readable name
description: <string>           # one-line summary
tags: [<strings>]               # categorization labels
version: "1.0"                  # playbook version

scope_ref: <string>             # EngagementMandate scope grant reference (sha256:<hex> or named scope)

phases:
  - name: <string>              # phase name (recon, enumerate, scan, verify, report)
    action: scanner|verify|report

    # scanner phases only:
    scanners:
      - adapter: <string>       # ScannerAdapter registry name (e.g. "nuclei", "nikto")
        config:                 # adapter-specific config
          ...

    # verify/report phases do NOT name ScannerAdapters:
    # (they use verifiers or report configuration instead)

    scope_ref: <string>         # inherited or phase-specific scope reference
    config:
      output_format: <string>   # sarif | json | markdown | html
      max_duration_seconds: <int>
      risk_threshold: <string>  # low | medium | high (scan/verify only)
```

## Phase Types

| action | scanners | scope_ref | config keys |
|--------|----------|-----------|-------------|
| `scanner` | Required — list of `ScannerAdapter` references | Required | output_format, max_duration_seconds, risk_threshold |
| `verify` | None — uses verifiers | Required | verifiers, confidence_threshold, report_format |
| `report` | None — generates deliverables | Required | sections, severity_threshold, output_format |

### Scanner Phase
- Names concrete `ScannerAdapter` implementations (e.g. `nuclei`, `sqlmap`, `nikto`)
- Each scanner carries its own `config` dict (adapter-specific)
- Gates on `EngagementMandate` scope check before dispatch

### Verify Phase
- No `ScannerAdapter` references — verification is policy-driven
- Uses `verifiers` list (retest, false_positive_elimination, manual_review)
- `confidence_threshold` controls auto-approval vs quarantine

### Report Phase
- No `ScannerAdapter` references — report generation is deterministic
- `sections` controls report structure
- `severity_threshold` controls which findings are promoted

## Scope Gating

Every phase `action` is gated on the `EngagementMandate` scope check:

1. The orchestrator resolves `scope_ref` to an `EngagementMandate` receipt
2. The receipt's `scope` is validated against the phase's declared target
3. If the scope check fails, the phase is skipped with a logged refusal
4. Phase config may override the inherited `scope_ref` for narrower sub-targets

## Example

See `web-app-full.yaml` for a complete 5-phase engagement:

```bash
bernstein run --playbook templates/engagements/web-app-full.yaml
```
