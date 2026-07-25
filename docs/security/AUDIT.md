# SOC 2 Audit Mode

Bernstein includes a SOC 2-compatible audit mode that creates a tamper-evident, append-only audit trail of every orchestrator action.

> Operator guide: see [`audit-log.md`](audit-log.md) for the record format, key
> management, rotation, verify/replay procedures, Merkle sealing, and SIEM setup.
> This page covers only the SOC 2 enabling switches and the evidence-export surface;
> all chain mechanics live in the operator guide so there is one source of truth.

## Quick Start

```bash
# Enable audit mode when running the orchestrator
bernstein run --audit

# View recent audit events
bernstein audit show

# Verify the audit log integrity
bernstein audit verify

# Export a SOC 2 evidence package
bernstein audit export --period Q1-2026
```

## Enabling audit mode

Audit mode is controlled by:

1. **CLI flag**: `bernstein run --audit`
2. **Config file**: set `audit_mode: true` in `bernstein.yaml`
3. **Compliance preset**: `bernstein run --compliance development` (includes audit)

Once enabled, every orchestrator action (task creation, state transitions, agent spawns, completions) is written to an HMAC-SHA256 chained, daily-rotated JSONL log under `.sdd/audit/`. The signing key is resolved from `BERNSTEIN_AUDIT_KEY_PATH`, then `$XDG_STATE_HOME/bernstein/audit.key`, then `~/.local/state/bernstein/audit.key`, and lives deliberately outside `.sdd` so it is isolated from the log volume.

For the record format, the HMAC chain construction, key resolution and rotation, Merkle sealing, verification, retention, and SIEM export, see the [audit-log operator guide](audit-log.md).

## SOC 2 Evidence Export

Generate a complete evidence package for auditors:

```bash
bernstein audit export --period Q1-2026 --format zip
```

The package includes:

- All audit log files for the period
- HMAC verification results
- Merkle seal records
- Compliance configuration snapshot
- WAL (Write-Ahead Log) entries
- SBOM (Software Bill of Materials)

## File Locations

| File | Purpose |
|------|---------|
| `.sdd/audit/*.jsonl` | Daily audit log files |
| `~/.local/state/bernstein/audit.key` (overridable via `BERNSTEIN_AUDIT_KEY_PATH` or `$XDG_STATE_HOME`) | HMAC signing key, kept outside `.sdd` (see [audit-log.md](audit-log.md#key-management)) |
| `.sdd/audit/merkle/` | Merkle tree seal records |
| `.sdd/audit/archive/` | Compressed old logs |
| `.sdd/evidence/article12_<bundle_id>.zip` | Article 12 evidence bundle (deterministic zip with manifest, events, data catalog, clause map). |

## Related

- [Audit-log operator guide](audit-log.md) - record format, key management, rotation,
  verify, Merkle seal, SIEM, recovery.
- [Multi-tenant audit-chain export](audit-multitenant.md) - per-tenant slice with
  optional RFC 3161 timestamping.
- [Delegation narrowing](delegation-narrowing.md) - recomputing child-subset-of-parent
  authority, separation of duties, and decision-time charter binding from delegation
  receipts.
- [DSSE / in-toto envelope](audit-dsse-envelope.md) - third-party-verifiable wrapper
  over the bundle.
- [EU AI Act Article 12 evidence pack](../compliance/eu-ai-act-article-12-bundle.md)
  - operator guide for the bundle this page references.
