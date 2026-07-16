# SOC 2 Audit Mode

Bernstein includes a SOC 2-compatible audit mode that creates a tamper-evident, append-only audit trail of every orchestrator action.

> Operator guide: see [`audit-log.md`](audit-log.md) for key
> management, rotation, verify/replay procedures, and SIEM setup.

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

## How It Works

### HMAC-Chained Audit Log

Every action in the orchestrator (task creation, state transitions, agent spawns, completions, etc.) is logged as a structured JSON event with an HMAC-SHA256 signature chained to the previous event:

```json
{
  "timestamp": "2026-04-01T14:30:00Z",
  "event_type": "task.transition",
  "actor": "orchestrator",
  "resource_type": "task",
  "resource_id": "TASK-001",
  "details": {
    "from_status": "open",
    "to_status": "claimed"
  },
  "prev_hmac": "a1b2c3...",
  "hmac": "d4e5f6..."
}
```

Each event's HMAC is computed over the canonical JSON payload concatenated with the previous event's HMAC. This creates a tamper-evident chain: modifying any event invalidates all subsequent HMACs.

### Merkle Tree Sealing

Periodically (or on demand), Bernstein computes a Merkle root hash across all audit log files. This seal can be:

- Stored locally in `.sdd/audit/merkle/`
- Anchored to a Git commit for external verification
- Included in SOC 2 evidence packages

```bash
# Compute and store a Merkle seal
bernstein audit seal

# Anchor the seal to Git
bernstein audit seal --anchor-git
```

### Daily Log Rotation

Audit logs are rotated daily, producing one JSONL file per day (e.g., `2026-04-01.jsonl`). The HMAC chain carries across file boundaries.

### Retention & Archiving

Old audit logs are automatically compressed and archived:

```python
# Default: 90-day retention, then gzip-compress to archive/
from bernstein.core.audit import AuditLog, RetentionPolicy

log = AuditLog(audit_dir=Path(".sdd/audit"))
log.archive(RetentionPolicy(retention_days=90))
```

## Verification

### HMAC Chain Verification

Walks all JSONL files and re-computes every HMAC to verify the chain is intact:

```bash
bernstein audit verify
bernstein audit verify --hmac-only
```

### Merkle Tree Verification

Recomputes the Merkle root and compares against stored seals:

```bash
bernstein audit verify --merkle-only
```

### Querying Events

Filter audit events by type, actor, or time range:

```bash
bernstein audit query --event-type task.transition --since 2026-03-01
bernstein audit query --actor orchestrator --limit 50
```

## Configuration

Audit mode is controlled by:

1. **CLI flag**: `bernstein run --audit`
2. **Config file**: Set `audit_mode: true` in `bernstein.yaml`
3. **Compliance preset**: `bernstein run --compliance development` (includes audit)

The HMAC key is automatically generated on first use. It is resolved from, in order: `BERNSTEIN_AUDIT_KEY_PATH`, then `$XDG_STATE_HOME/bernstein/audit.key`, then `~/.local/state/bernstein/audit.key`. The key lives deliberately outside `.sdd` so it is isolated from the log volume. Protect this file - it's required for verification. See ["Key management" in audit-log.md](audit-log.md#key-management).

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

- [Audit-log operator guide](audit-log.md) - key management, rotation,
  verify, SIEM, recovery.
- [Multi-tenant audit-chain export](audit-multitenant.md) - per-tenant
  slice with optional RFC 3161 timestamping.
- [DSSE / in-toto envelope](audit-dsse-envelope.md) -
  third-party-verifiable wrapper over the bundle.
- [EU AI Act Article 12 evidence pack](../compliance/eu-ai-act-article-12-bundle.md)
  - operator guide for the bundle this page references.
