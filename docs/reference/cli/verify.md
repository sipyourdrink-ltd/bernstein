# `bernstein verify`

`bernstein verify` is five independent verification modes bundled behind
one command: air-gap wheelhouse signatures, WAL hash-chain integrity,
execution-determinism fingerprints, lesson-memory provenance, and formal
property checks. Each mode is selected by its own flag (or a positional
argument for wheelhouse mode); passing more than one runs all of them and
combines their exit codes with bitwise OR.

This command is not the audit-log verifier — for the HMAC-chained,
Merkle-sealed audit trail, see [`bernstein audit verify`](../../security/audit-log.md).

## Usage

```bash
bernstein verify <wheelhouse-path>                          # air-gap wheelhouse signatures
bernstein verify --wal-integrity <run-id>                   # WAL hash-chain check
bernstein verify --determinism <run-id>                     # print execution fingerprint
bernstein verify --determinism <run-id> --expect <digest>   # gate on a recorded fingerprint
bernstein verify --determinism <run-b> --baseline <run-a>   # gate that run-b reproduces run-a
bernstein verify --memory-audit                              # audit lesson-memory provenance
bernstein verify --formal <task-id>                          # Z3/Lean4 property checks
```

Running the bare command with no arguments prints a usage hint and returns
without error.

## Modes

### Wheelhouse signature verification

```bash
bernstein verify ./airgap-wheelhouse/1.10.0
```

Verifies every wheel's SHA-256 against `MANIFEST.json` and, when signature
files are present or `--require-signatures` is set, validates cosign / GPG
/ PEM-key signatures. Optional flags add a customer-key countersignature
check (`--require-customer-sig`) and Sigstore build-provenance verification
(`--sigstore`, `--sigstore-offline`, `--require-sigstore`). This mode is the
one covered in full in the [air-gap installation guide](../../installation/air-gap.md) —
see that page for the complete flag reference and troubleshooting table.

### WAL integrity (`--wal-integrity RUN_ID`)

Reads `.sdd/runtime/wal/<run-id>.wal.jsonl` and replays its hash chain
(`WALReader.verify_chain()`). Exits 0 with an entry count when the chain is
intact, 1 with the list of chain errors when it isn't, and 1 with a "WAL
file not found" message when the run has no WAL.

### Execution determinism (`--determinism RUN_ID`)

Computes an `ExecutionFingerprint` from the same WAL and prints it. Two
optional gates change the exit code:

| Gate | Behaviour |
|---|---|
| (none) | Bare mode: print the fingerprint, exit 0. |
| `--expect DIGEST` | Constant-time compare against `DIGEST`; exit 0 on match, 2 on mismatch (prints both digests). |
| `--baseline RUN_ID` | Compare the fingerprint against a second run's; exit 0 on match, 2 on mismatch, and names the first diverging WAL entry. |

`--expect` and `--baseline` are mutually exclusive and both require
`--determinism`. A green gate proves the two runs' WAL *decision traces*
matched — it does not prove on-disk artefacts are byte-identical.

### Lesson-memory provenance (`--memory-audit`)

Walks `.sdd/memory/lessons.jsonl` and verifies its hash chain
(`verify_chain`) plus a per-entry provenance trail (`audit_provenance`),
reporting counts of hash-tampered and chain-mispositioned entries. Exits 0
when clean (or when no lesson memory file exists yet) and 1 on any
violation. This check exists to satisfy OWASP Agent Security Initiative
ASI06 (Memory & Context Poisoning).

### Formal property checks (`--formal TASK_ID`)

Fetches the named task from the running task server and runs the property
checks declared in `bernstein.yaml`'s `formal_verification` section against
it via Z3 / Lean4. Exits 0 if the section is absent, disabled, or has no
properties defined (nothing to check); exits 0 on a pass and 1 on any
violation, printing each violated property and its counterexample (if one
was found before the checker timed out).

The CLI surface ships with Bernstein; the Z3 and Lean4 binaries themselves
must be installed separately and on `PATH` — they are not bundled.

## Source

`src/bernstein/cli/commands/verify_cmd.py`.
