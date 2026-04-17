# WORKFLOW: SOC 2 Evidence Export Package
**Version**: 1.0
**Date**: 2026-04-08
**Author**: Workflow Architect
**Status**: Draft
**Implements**: ENT-004

---

## Overview

Transforms the raw JSONL audit export (`bernstein audit export --period Q1-2026`) into a structured SOC 2 compliance package. The package includes control mappings (CC6.1, CC7.2, etc.), evidence summaries with narrative text for auditors, Merkle root attestation, and a structured output format suitable for PDF rendering. The workflow is triggered by the existing CLI command with an enhanced output pipeline.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Operator | Invokes `bernstein audit export --period Q1-2026 --format zip` |
| CLI (`audit_cmd.py`) | Parses arguments, validates period, delegates to export function |
| `compliance.py` (`export_soc2_package`) | Collects artifacts, writes manifest |
| `audit.py` (`AuditLog.verify`) | Runs full HMAC chain verification for the package |
| `merkle.py` | Computes/loads Merkle seals for attestation |
| Audit log files (`.sdd/audit/*.jsonl`) | Source event data filtered by period |
| Config files (`.sdd/config/`) | Compliance configuration, policy docs |
| WAL files (`.sdd/runtime/wal/`) | Write-ahead log entries for evidence |
| SBOM files (`.sdd/sbom/`) | Software bill of materials |

---

## Prerequisites

- `.sdd/` directory exists with at least one orchestrator run completed
- Audit log files exist in `.sdd/audit/` for the requested period
- HMAC key exists at `.sdd/config/audit-key` (for chain verification)
- Period string is parseable (`Q1-2026`, `2026-03`, `2026`)

---

## Trigger

CLI command: `bernstein audit export --period <PERIOD> [--format zip|dir] [-o OUTPUT]`

Entry point: `audit_cmd.py:export_cmd()` -> `compliance.py:export_soc2_package()`

---

## Workflow Tree

### STEP 1: Parse and validate period

**Actor**: CLI (`audit_cmd.py`)
**Action**: Parse the `--period` argument into ISO 8601 start/end dates using `parse_period()`.
**Timeout**: N/A (string parsing)
**Input**: `period: str` (e.g. `"Q1-2026"`)
**Output on SUCCESS**: `(start_date="2026-01-01", end_date="2026-03-31")` -> GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(ValueError)`: Invalid period format -> Print error, exit 1. No cleanup needed.

**Observable states during this step**:
- Operator sees: Nothing (or error message if invalid)
- Logs: None

---

### STEP 2: Validate state directory

**Actor**: CLI (`audit_cmd.py`)
**Action**: Check that `.sdd/` exists and contains audit data.
**Input**: `sdd_dir: Path`
**Output on SUCCESS**: `.sdd/` exists -> GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(not_found)`: `.sdd/` does not exist -> Print "Run bernstein run first", exit 1

---

### STEP 3: Collect audit logs for period

**Actor**: `compliance.py`
**Action**: Scan `.sdd/audit/*.jsonl` files. Filter by filename date (filenames are `YYYY-MM-DD.jsonl`). Copy matching files to `bundle_dir/audit_logs/`.
**Timeout**: 60s (large audit directories)
**Input**: `audit_dir: Path`, `start_date: str`, `end_date: str`
**Output on SUCCESS**: Audit log files copied, artifact entry added -> GO TO STEP 4
**Output on EMPTY**: No files match the period -> Continue (warning in manifest, no audit_logs artifact)
**Output on FAILURE**:
  - `FAILURE(IOError)`: File copy failed -> ABORT_CLEANUP

**Observable states during this step**:
- Database: Files being copied to temp bundle directory
- Logs: `[INFO] Collected N audit log files for period`

---

### STEP 4: Run HMAC chain verification

**Actor**: `audit.py` (`AuditLog.verify()`)
**Action**: Instantiate `AuditLog(audit_dir)` and run `verify()` across all JSONL files. Record result in `verification.json`.
**Timeout**: 120s (full chain verification can be slow on large logs)
**Input**: `audit_dir: Path`
**Output on SUCCESS** (`valid=True`): Verification passes -> GO TO STEP 5
**Output on FAILURE** (`valid=False`): Chain integrity errors found -> Still GO TO STEP 5 (record the failure, don't abort — auditors need to see the failure)
**Output on EXCEPTION**: Verification threw -> Record error in `verification.json`, GO TO STEP 5

**Observable states during this step**:
- Logs: `[INFO] HMAC chain verification: valid=True/False`

**Design decision — include failures in package**:
A SOC 2 package must honestly report the state of controls. If HMAC verification fails, that is itself evidence that the control was tested and found deficient. Suppressing the failure would be worse than reporting it.

---

### STEP 5: Collect Merkle seals and compute attestation

**Actor**: `compliance.py` + `merkle.py`
**Action**:
  1. Copy existing Merkle seal JSON files from `.sdd/audit/merkle/` to `bundle_dir/merkle_seals/`
  2. **NEW**: Compute a fresh Merkle seal over the audit files included in this package using `compute_seal()`
  3. **NEW**: Write `merkle_attestation.json` containing the current root hash, leaf count, algorithm, and timestamp
**Timeout**: 30s
**Input**: `audit_dir: Path`, collected audit files
**Output on SUCCESS**: Merkle seals copied, attestation written -> GO TO STEP 6
**Output on SKIP**: No Merkle directory exists -> Continue without seals (warning logged)

---

### STEP 6: Map evidence to SOC 2 controls

**Actor**: `compliance.py` (NEW function: `build_control_mappings`)
**Action**: Generate a structured mapping between collected evidence artifacts and SOC 2 Trust Services Criteria (TSC). This is the core value-add over the current raw export.

**Control mapping table**:

| Control | Title | Evidence artifacts | Narrative |
|---|---|---|---|
| CC6.1 | Logical and Physical Access Controls | `audit_logs/` (access events), `compliance_config/` | HMAC-chained audit log records all access to orchestrated tasks, agents, and resources. Each event includes actor identity, timestamp, and resource affected. |
| CC6.2 | Prior to Issuing System Credentials | `compliance_config/` (auth settings), `audit_logs/` (agent token events) | Agent tokens are issued per-session with scoped permissions. Token lifecycle events are recorded in the audit log. |
| CC6.3 | Registration and Authorization of New Users | `audit_logs/` (agent registration events) | Agent registration and authorization are logged. Each agent session receives a unique token. |
| CC7.1 | Detection and Monitoring | `audit_logs/`, `verification.json`, `merkle_attestation.json` | Continuous monitoring via HMAC-chained audit events. Merkle tree integrity seals provide tamper-evident checkpoints. HMAC verification runs on startup. |
| CC7.2 | Monitoring of System Components | `wal/` (write-ahead log), `audit_logs/` (lifecycle events) | Write-ahead log captures all task state transitions. System health is monitored via heartbeat protocol. |
| CC7.3 | Evaluation of Identified Vulnerabilities | `sbom/` (CycloneDX SBOM), `audit_logs/` (security events) | Software bill of materials tracks all dependencies. Dependency vulnerability scanning is integrated. |
| CC8.1 | Changes to Infrastructure and Software | `audit_logs/` (task transitions), `wal/` | All code changes by agents are tracked via task lifecycle events. WAL provides crash-safe decision history. |

**Output format**: `control_mappings.json` written to `bundle_dir/`
```json
{
  "framework": "SOC 2 Type II",
  "trust_services_criteria": "2017",
  "period": "Q1-2026",
  "controls": [
    {
      "control_id": "CC6.1",
      "title": "Logical and Physical Access Controls",
      "evidence_files": ["audit_logs/2026-01-15.jsonl", "..."],
      "evidence_summary": "...",
      "assessment": "effective|not_effective|not_applicable",
      "gaps": []
    }
  ]
}
```
**Output on SUCCESS**: -> GO TO STEP 7

---

### STEP 7: Generate evidence summary

**Actor**: `compliance.py` (NEW function: `generate_evidence_summary`)
**Action**: Produce a human-readable evidence summary document suitable for auditor review. This aggregates statistics from the collected artifacts.

**Summary includes**:
1. **Executive summary**: Period, system description, compliance preset active
2. **Audit log statistics**: Total events, event types breakdown, unique actors, date range covered
3. **Integrity attestation**: HMAC chain verification result, Merkle root hash, seal count
4. **Control effectiveness**: Per-control summary from Step 6 mappings
5. **Gaps and findings**: Any controls without sufficient evidence

**Output format**: `evidence_summary.json` written to `bundle_dir/`
```json
{
  "executive_summary": {
    "system": "Bernstein Orchestrator",
    "period": "Q1-2026 (2026-01-01 to 2026-03-31)",
    "compliance_preset": "REGULATED",
    "generated_at": "2026-04-08T14:30:00Z"
  },
  "audit_statistics": {
    "total_events": 15432,
    "event_types": {"task.transition": 8721, "agent.spawn": 2100, "...": "..."},
    "unique_actors": 47,
    "first_event": "2026-01-01T00:12:34Z",
    "last_event": "2026-03-31T23:58:12Z"
  },
  "integrity": {
    "hmac_chain_valid": true,
    "merkle_root": "a1b2c3...",
    "merkle_leaf_count": 90,
    "verification_timestamp": "2026-04-08T14:30:05Z"
  },
  "control_summary": {
    "total_controls": 7,
    "effective": 6,
    "not_effective": 0,
    "not_applicable": 1,
    "gaps": []
  }
}
```
**Output on SUCCESS**: -> GO TO STEP 8

---

### STEP 8: Collect supporting artifacts

**Actor**: `compliance.py` (existing logic)
**Action**: Copy remaining artifacts into the bundle:
  1. Compliance configuration from `.sdd/config/` (excluding `audit-key` — NEVER include the secret key)
  2. WAL entries from `.sdd/runtime/wal/`
  3. SBOM files from `.sdd/sbom/`
**Timeout**: 30s
**Input**: `sdd_dir: Path`
**Output on SUCCESS**: Artifacts copied -> GO TO STEP 9
**Output on PARTIAL**: Some artifact directories don't exist -> Continue with available artifacts (warning in manifest)

**Security constraint**: The `audit-key` file MUST be excluded. Including it in the export would allow anyone to forge audit entries.

---

### STEP 9: Compute package checksums

**Actor**: `compliance.py` (existing logic)
**Action**: SHA-256 hash every file in the bundle directory. Store in `manifest.json` under `file_checksums`.
**Timeout**: 30s
**Input**: `bundle_dir: Path`
**Output on SUCCESS**: Checksums computed -> GO TO STEP 10

---

### STEP 10: Write manifest

**Actor**: `compliance.py`
**Action**: Write `manifest.json` as the package's authoritative index. The manifest ties together all artifacts, verification results, control mappings, and checksums.

**Manifest schema** (enhanced from current):
```json
{
  "package_type": "soc2-evidence",
  "package_version": "2.0",
  "period": "Q1-2026",
  "period_start": "2026-01-01",
  "period_end": "2026-03-31",
  "exported_at": "2026-04-08T14:30:10Z",
  "system": "Bernstein Orchestrator",
  "compliance_preset": "REGULATED",
  "artifacts": [
    {"type": "audit_logs", "description": "...", "file_count": 90},
    {"type": "control_mappings", "description": "SOC 2 control-to-evidence mappings", "file_count": 1},
    {"type": "evidence_summary", "description": "Aggregated evidence statistics and narrative", "file_count": 1},
    {"type": "merkle_attestation", "description": "Merkle root attestation for package integrity", "file_count": 1}
  ],
  "verification": {
    "hmac_chain": {"valid": true, "errors": []},
    "merkle": {"root_hash": "...", "leaf_count": 90}
  },
  "control_mappings_ref": "control_mappings.json",
  "evidence_summary_ref": "evidence_summary.json",
  "file_checksums": {"audit_logs/2026-01-01.jsonl": "sha256:...", "...": "..."}
}
```
**Output on SUCCESS**: -> GO TO STEP 11

---

### STEP 11: Package output (zip or directory)

**Actor**: `compliance.py` (existing logic)
**Action**: If `--format zip`, create a zip archive from the bundle directory and delete the temp directory. If `--format dir`, leave the directory in place.
**Timeout**: 60s (zip of large bundles)
**Input**: `bundle_dir: Path`, `fmt: str`
**Output on SUCCESS** (`zip`): Zip file at `<output>/soc2-Q1-2026.zip` -> GO TO STEP 12
**Output on SUCCESS** (`dir`): Directory at `<output>/soc2-Q1-2026/` -> GO TO STEP 12
**Output on FAILURE**:
  - `FAILURE(IOError)`: Zip creation failed -> ABORT_CLEANUP

---

### STEP 12: Display summary to operator

**Actor**: CLI (`audit_cmd.py`)
**Action**: Print Rich-formatted summary table showing period, format, output path, artifact count, and integrity status.
**Output**: -> DONE (exit 0)

**Observable states during this step**:
- Operator sees: Rich panel with package details
- Logs: `[INFO] SOC 2 evidence package exported: <path>`

---

### ABORT_CLEANUP

**Triggered by**: File I/O failures in Steps 3, 8, 11
**Actions** (in order):
  1. Remove the temp bundle directory (`shutil.rmtree(bundle_dir)`)
  2. Print error message to operator
  3. Exit 1
**What operator sees**: Error message, non-zero exit code

---

## State Transitions

```
[cli_invoked] -> (period valid, .sdd exists) -> [collecting]
[collecting] -> (all artifacts collected) -> [verifying]
[verifying] -> (HMAC + Merkle done) -> [mapping_controls]
[mapping_controls] -> (controls mapped, summary generated) -> [packaging]
[packaging] -> (zip/dir created) -> [exported]
[any_step] -> (I/O failure) -> [failed] (cleanup, exit 1)
```

---

## Handoff Contracts

### CLI -> export_soc2_package()

**Function call**: `export_soc2_package(sdd_dir, period, output_path, fmt)`
**Input**:
```python
sdd_dir: Path       # e.g. Path("/project/.sdd")
period: str          # e.g. "Q1-2026"
output_path: Path | None  # custom output dir, or None for default
fmt: str             # "zip" or "dir"
```
**Success response**: `Path` to the exported zip file or directory
**Failure response**: `ValueError` raised with descriptive message
**Timeout**: Caller does not enforce — function is synchronous

### export_soc2_package() -> AuditLog.verify()

**Function call**: `AuditLog(audit_dir).verify()`
**Success response**: `(True, [])` — chain valid
**Failure response**: `(False, ["file:line: error description", ...])` — chain broken
**Timeout**: None enforced (should be — see OQ-2)

### export_soc2_package() -> compute_seal() (NEW)

**Function call**: `compute_seal(audit_dir)` from `merkle.py`
**Success response**: `(MerkleTree, seal_dict)` — tree computed
**Failure response**: `ValueError` if no files found

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Bundle directory (`soc2-Q1-2026/`) | Step 3 | ABORT_CLEANUP or Step 11 (zip mode) | `shutil.rmtree()` |
| Zip file (`soc2-Q1-2026.zip`) | Step 11 | Operator (manual) | File delete |

---

## Package Directory Structure

```
soc2-Q1-2026/
  manifest.json                 # Package index with checksums
  evidence_summary.json         # Aggregated statistics + narrative
  control_mappings.json         # SOC 2 TSC control-to-evidence map
  verification.json             # HMAC + Merkle verification results
  merkle_attestation.json       # Current Merkle root attestation
  audit_logs/
    2026-01-01.jsonl
    2026-01-02.jsonl
    ...
    2026-03-31.jsonl
  merkle_seals/
    seal-<timestamp>.json
    ...
  compliance_config/
    compliance.json
    ...
  wal/
    <wal-files>
  sbom/
    sbom-<run-id>.cdx.json
    ...
```

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path (zip) | Valid period, all artifacts exist | Zip file created with manifest, all sections populated |
| TC-02: Happy path (dir) | `--format dir` | Directory created, not zipped |
| TC-03: Invalid period | `--period foobar` | ValueError, exit 1, no artifacts created |
| TC-04: No `.sdd/` directory | Fresh project | Error message, exit 1 |
| TC-05: No audit files for period | `--period Q4-2030` (future) | Package created with empty audit_logs, warning in manifest |
| TC-06: HMAC verification fails | Tampered audit entries | Package includes `verification.json` with `valid=false`, control CC7.1 marked `not_effective` |
| TC-07: No Merkle seals | No `.sdd/audit/merkle/` | Package created without merkle_seals dir, attestation computed fresh |
| TC-08: No SBOM | No `.sdd/sbom/` | Package created without sbom dir |
| TC-09: audit-key excluded | Config dir has `audit-key` | Key file NOT copied to compliance_config |
| TC-10: Control mappings present | Any successful export | `control_mappings.json` contains CC6.1, CC7.2 entries |
| TC-11: Evidence summary statistics | Export with 100 audit events | `evidence_summary.json` shows `total_events=100` and event type breakdown |
| TC-12: Checksums valid | Any successful export | Every file in bundle matches its SHA-256 in `manifest.json` |
| TC-13: Existing bundle overwritten | Run export twice for same period | Previous bundle removed before new one created |
| TC-14: Custom output path | `-o /tmp/evidence` | Package written to `/tmp/evidence/soc2-Q1-2026.zip` |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Audit log filenames follow `YYYY-MM-DD.jsonl` format | Verified: `audit.py:188-189` generates this format | Files with other names are silently excluded from period filter |
| A2 | `audit-key` is the only sensitive file in `.sdd/config/` | Partially verified: current code only creates `audit-key` and `compliance.json` | Future config files with secrets would leak |
| A3 | SOC 2 TSC 2017 criteria are the target framework | Assumed — no explicit framework version in codebase | Control IDs may differ across TSC revisions |
| A4 | PDF-ready means structured JSON that a renderer can consume | Assumed — no PDF library dependency exists | If actual PDF output is needed, a dependency like `weasyprint` or `reportlab` would be required |
| A5 | The operator has sufficient disk space for the bundle + zip | Not checked | Large audit logs could exhaust disk during zip creation |
| A6 | WAL files are not period-filtered (all WAL entries included) | Verified: current code copies all WAL files | WAL from outside the period is included — may or may not be desired |

## Open Questions

- **OQ-1**: Should WAL entries be filtered by period like audit logs, or should the full WAL always be included? Current implementation copies all WAL files regardless of period.
- **OQ-2**: Should there be a timeout on `AuditLog.verify()` during export? A very large audit log could make the export take minutes. Consider adding a `--skip-verify` flag for speed.
- **OQ-3**: Should the control mapping be configurable or extensible? Different organizations may map to different SOC 2 controls. Consider allowing a `control_mappings.yaml` override in `.sdd/config/`.
- **OQ-4**: Is JSON sufficient as "PDF-ready" output, or is actual PDF generation required? JSON can be rendered by tools like Pandoc, but a self-contained PDF would be more convenient for auditors.
- **OQ-5**: Should the package include a separate `README.txt` explaining the package structure to auditors who may not be familiar with JSONL or Merkle trees?

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-08 | Initial spec created. Current `export_soc2_package()` collects raw artifacts but has no control mappings, evidence summaries, Merkle attestation, or structured formatting. | Spec defines 6 new artifacts/enhancements needed. |
| 2026-04-08 | `audit-key` exclusion exists in current code (`compliance.py:613`). | Verified — security constraint is already implemented. |
| 2026-04-08 | `parse_period()` has a bug: February always returns 28 or 29 days but doesn't handle Q1 end date correctly (Q1 ends March 31, not March 30). | Existing code handles Q1 correctly (end_month=3, end_day=31). No bug. |
| 2026-04-08 | Current `export_soc2_package` deletes existing bundle dir before rebuilding (`shutil.rmtree`). | Verified at `compliance.py:548`. Operator loses previous export — spec adds TC-13 for this. |
