# `bernstein verify`

`bernstein verify` is a command group: two run-receipt subcommands
(`run` and `receipt`, issue #2924), the verifier-ladder subcommand
(`ladder`, issue #2927), plus five legacy verification modes —
air-gap wheelhouse signatures, WAL hash-chain integrity,
execution-determinism fingerprints, lesson-memory provenance, and formal
property checks. The legacy modes live on the default `legacy` subcommand:
any invocation whose first token is not `run` / `receipt` / `ladder` /
`legacy` routes there, so pre-group invocations keep their exact behaviour
and exit codes.
Each legacy mode is selected by its own flag (or a positional argument for
wheelhouse mode); passing more than one runs all of them and combines their
exit codes with bitwise OR.

This command is not the audit-log verifier — for the HMAC-chained,
Merkle-sealed audit trail, see [`bernstein audit verify`](../../security/audit-log.md).

## Usage

```bash
bernstein verify run <run-id> --signing-key-path key.pem    # build the signed run receipt
bernstein verify receipt <path> [--public-key pub.pem]      # verify a receipt offline (0/1/2)
bernstein verify ladder <receipt-hash>                      # re-derive a verifier-ladder receipt (0/1/2)
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

One routing edge: a wheelhouse directory literally named `run`, `receipt`,
`ladder`, or `legacy` shadows the positional mode — spell it `./run` or use
`bernstein verify legacy <path>`.

## Run receipts

### Build (`verify run RUN_ID`)

Builds an Ed25519-signed `run-receipt.json` under
`.sdd/runs/<run-id>/` binding the run's journal head (the exact journal-state
identifier, not by itself a finished-journal completeness claim),
lineage-spine head (artifact provenance), and — opt-in via
`--include-audit-range --audit-since --audit-until` — a re-chained
audit-chain slice under one signed subject, with the public key embedded as
an RFC 7517 OKP/Ed25519 JWK. The signing key comes from
`--signing-key-path` (PEM PKCS#8 or raw 32-byte Ed25519) or
`--signing-env-var`, falling back to
`$BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH` /
`$BERNSTEIN_RUN_RECEIPT_SIGNING_ENV_VAR` — the same env configuration the
orchestrator uses to write a receipt automatically at run finalization
(a documented no-op when no key is configured; receipts are never emitted
unsigned). Exits 0 on success, 1 when the run has no journal events or the
key cannot load, 2 on usage errors (no key configured, conflicting flags,
missing audit window).

### Verify (`verify receipt PATH [--public-key PEM]`)

Verifies a receipt from the file: recomputes the journal head from the
embedded timing-excluded rows (the exact `verify_journal` walk), recomputes
every spine `entry_hash` and the spine head without any HMAC key, recomputes
the optional audit-range `head_sha256` from its embedded events, rebuilds
the signed subject from those recomputed values, and checks the Ed25519
signature. No HMAC key and no `.sdd/` are read.

What a pass proves depends on where the key came from, and the verdict is
labelled accordingly:

- **Without `--public-key`** the signature is checked against the key
  embedded in the receipt (trust-on-first-use) and the verdict reads
  `OK (integrity-only: embedded key)`. This proves the file is internally
  consistent — any post-signing mutation is caught at a precise step — but
  not *who* signed it: a forger controlling the whole file could re-sign
  with their own embedded key.
- **With `--public-key`** the embedded key must match the pinned
  out-of-band Ed25519 public key and the verdict reads
  `OK (provenance: pinned key)`. Provenance-sensitive review should always
  pin.

| Exit code | Meaning |
|---|---|
| 0 | Every head recomputes from the embedded ranges and the signature verifies. |
| 1 | Empty or malformed input (unreadable file, missing ranges or fields). |
| 2 | Tamper detected — the first divergent journal step index is named (a pinned-key mismatch also exits 2). |

Full format description:
[deterministic replay](../../operations/deterministic-replay.md#signed-run-receipt-one-file-offline-verification).

## Ladder receipts (`verify ladder RECEIPT_HASH`)

Re-derives a pre-merge verifier-ladder receipt (issue #2927) instead of
trusting it. The receipt — written by the janitor under
`.sdd/quality/ladder/` when it runs with a `VerifierLadderContext` — carries
one sealed record per verifier tier that actually executed (`deterministic`
/ `judge` / `human`) and a composite `merge_eligible` claim. Verification
re-hashes the stored body, re-runs the pure fail-closed verdict derivation
over the stored tier verdicts (a stored claim those verdicts do not entail
is rejected even when the receipt's hashes are internally consistent), and
re-checks every tier's `spine_entry_hash` against the `verifier-ladder`
lineage spine's content hashes, so a substituted or dangling tier record
fails by name. The command prints per-tier
`tier / config_hash / evidence_hash / verdict` plus the composite result.

Reads the project audit HMAC key (the spine key) and `.sdd/` under
`--workdir`; a removed or tampered spine fails closed — without the
substrate no tier can be confirmed to have run.

| Exit code | Meaning |
|---|---|
| 0 | The receipt verifies and its composite claim is entailed by its tier verdicts. |
| 1 | No readable receipt for the hash. |
| 2 | Re-derivation or spine-anchor mismatch (tamper). |

Architecture: [verifier ladder](../../sdd/verifier-ladder.md).

## Legacy modes

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

## Unified Verification Exit-Code Reference

Every verification command in Bernstein follows a strict exit-code contract. The table below covers all primary verification commands, their exit codes, verdict output markers, and failure conditions:

| Command | Exit Code | Verdict / Output Marker | Condition / Meaning |
|---|---|---|---|
| `replay <run> --verify` | `0` | `Receipt verified:` / `No divergence; chains match end-to-end.` | Execution trace intact, receipt signature valid, no step divergence |
| | `1` | `Receipt failed verification` / `Divergence at step <N>` | Step divergence detected or chain hash mismatch |
| | `2` | `One or both journals are missing.` / `Cannot load public key:` | Usage error, missing journal files, or unreadable key file |
| `verify receipt <path>` | `0` | `OK (provenance: pinned key)` / `OK (integrity-only: embedded key)` | Receipt verified: all embedded heads recompute and Ed25519 signature checks |
| | `1` | `MALFORMED` | Unreadable receipt file or missing required fields/ranges |
| | `2` | `TAMPER DETECTED` | Step divergence, spine/audit head mismatch, signature or pinned-key mismatch |
| `lineage verify <run>` | `0` | `OK` | Lineage spine/chain intact and non-empty, all HMAC tags / signatures valid |
| | `1` | `NO ENTRIES` / `SEAL ONLY` | Empty run emitted no lineage (1), or chain records only journal-head seal with no artifact provenance (1) |
| | `2` | `TAMPER DETECTED` / `RECEIPT VERIFICATION FAILED` | HMAC tag mismatch, broken Merkle chain, or recovery receipt resolution failed |
| | `3` | `CANNOT VERIFY` | Audit HMAC key file missing (read-only verification safety fail-closed) |
| `audit verify` | `0` | `Passed` across all pillars | All audit log pillars (HMAC chain, Merkle tree, checkpoints, evidence, artifacts, receipts, gates, grants) pass |
| | `1` | `FAILED` / non-zero exit | Any audit pillar failed verification, broken HMAC chain, tear evidence, or missing audit directory |
| | `2` | `[red]--payload requires --receipt.[/red]` | Invalid flag combination / usage error |

## Source

`src/bernstein/cli/commands/verify_cmd.py` (command group);
`src/bernstein/core/replay/run_receipt.py` (receipt build + offline verify);
`src/bernstein/core/quality/verifier_ladder.py` (ladder receipts).

