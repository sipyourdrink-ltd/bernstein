# Adapter admission receipts

Bernstein resolves an adapter by name: a key in the registry produces a live
adapter that receives the task, the workspace, and the worker's credential
scope. Historically that resolution asked nothing about whether the adapter
had actually been verified against the binary installed on the host. An
adapter whose conformance verdict was `skip` — because it shipped no contract,
or because its binary was missing, or because the `--help` probe was
inconclusive — spawned exactly like one that had passed every check.

A skip is not a pass. Admission receipts make that distinction load-bearing.

## What it does

- **Admission is proof-based, not name-based.** The spawn path re-derives the
  adapter's evidence — the installed binary's version, the pinned contract's
  content hash, the capability profile's content hash, the golden-transcript
  replay outcome, and the nightly canary's attestation — and admits the
  adapter only when a sealed receipt pins that same evidence.
- **The refusal is a first-class record.** A skipped, stale, or failing
  adapter seals a *refusal* receipt naming the reason, the capabilities it is
  explicitly denied, and the remediation that would clear it. An absent
  admission can never be read as an implicit one.
- **Both paths are anchored.** Admissions and refusals alike are mirrored into
  the HMAC audit chain as `adapter.admission_receipt` events. Given a
  contiguous chain slice, an operator can prove offline which adapters held
  spawn authority during a window and, for each one that did not, why.
- **Drift is a named divergence.** The replay fingerprint is a deterministic
  projection of `(contract bytes, binary version, golden-transcript replay
  output)`. Two operators on the same binary version derive byte-identical
  bytes, so a binary that moved under a still-valid receipt is caught as a
  fingerprint mismatch rather than riding a stale attestation.
- **Stripping the receipt withdraws authority.** Deleting a sealed receipt
  makes the adapter un-spawnable under the enforce policy, not merely
  unlogged.

## Receipt fields

| Field | Meaning |
| --- | --- |
| `adapter`, `binary`, `binary_path` | Which adapter, and the binary the probe resolved. |
| `installed_version`, `probe_hash` | The upstream version seen, and a content hash binding the probed binary identity. |
| `contract_hash`, `profile_hash` | Content addresses of the pinned contract and the capability profile. |
| `replay_fingerprint` | Deterministic projection of contract bytes, binary version, and transcript replay. |
| `conformance_run_id` | Deterministic id of the conformance run behind the decision. |
| `conformance_verdict`, `canary_verdict` | The in-process probe verdict and the nightly attestation state. |
| `verdict`, `reason` | `admit` / `refuse`, and the refusal reason. |
| `allowed_capabilities`, `forbidden_capabilities` | What the decision grants and what it explicitly withholds. On a refusal, `spawn` itself is forbidden. |
| `admission_ttl_seconds` | Freshness window — one nightly-canary cycle. |
| `remediation` | The operator-visible next action. |

## Refusal reasons

| Reason | What happened |
| --- | --- |
| `no_contract` | No contract YAML pins the adapter's invocation surface. |
| `no_transcript` | No golden transcript replays the adapter's spawn path. |
| `conformance_skip` | The `--help` probe was inconclusive — installed, missing, or redesigned. |
| `conformance_fail` | The installed binary no longer advertises the required surface. |
| `replay_diverged` | The golden transcript no longer replays clean. |
| `canary_red` | The nightly attestation for the installed version is red. |
| `no_receipt` | Live evidence is green but nothing is sealed on disk. |
| `receipt_stale` | The sealed receipt is past its TTL. |
| `receipt_tampered` | The receipt body no longer hashes to its recorded identity. |
| `fingerprint_mismatch` | The binary, the contract, or the transcript changed after sealing. |
| `stored_refusal` | The sealed receipt itself records a refusal. |

## Policy

Warn-by-default: the gate records every decision from the first run so an
operator sees exactly which adapters would be refused before it starts
blocking. To make a refusal a hard stop:

```
export BERNSTEIN_ADAPTER_ADMISSION_POLICY=enforce
```

`=off` disables the gate entirely. `mock` and `generic` are always exempt —
neither wraps a pinned upstream surface, so offline work is never blocked.

## Operator commands

Check an adapter and exit non-zero when it is refused:

```
bernstein adapters verify claude
```

Seal and anchor a fresh receipt from the current evidence:

```
bernstein adapters verify claude --seal
```

Emit the raw receipt for a reviewer or a CI dashboard:

```
bernstein adapters verify claude --format json
```

Sealed receipts live under `.sdd/adapters/admission/` and are
content-addressed, so a tampered body no longer hashes to its recorded
identity and is rejected without any key material.

## Relationship to the other adapter gates

The admission gate sits alongside two existing spawn-path gates and does not
replace either:

- The [security floor](security-floor.md) refuses a binary below its
  minimum-safe version. That is a supply-chain question about *which build* is
  installed.
- Capability-aware routing refuses an adapter whose profile cannot satisfy a
  task's declared requirements. That is a fit question about *what the task
  needs*.
- Admission refuses an adapter that has not been verified against the binary
  actually installed. That is a trust question about *whether the surface was
  ever checked*.

All three seal receipts into the same HMAC chain, so one slice answers all
three questions.
