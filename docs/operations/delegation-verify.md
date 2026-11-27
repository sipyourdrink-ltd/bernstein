# Delegation chain verification

A run that fans out work carries delegated authority down the chain:
`principal -> orchestrator -> sub-agent`. Bernstein records two independent,
offline-verifiable trails of that authority — a per-hop receipt ledger (the
"ACT log": which principal authorized which sub-agent action) and a signed
capability-token chain (scoped, attenuating authority grants). `bernstein
delegation verify` and `bernstein delegation verify-token` check each trail
with no network call, no live coordinator, and no registry lookup.

The two surfaces are deliberately not coupled. A receipt may later reference
a token hash, and the receipt chain uses the run manifest for its final head declaration.

## `bernstein delegation verify` — the per-hop receipt chain

```
bernstein delegation verify RUN [--root DIR] [--json]
```

Reconstructs the delegation receipt ledger for `RUN` from
`<root>/delegation/<run_id>.jsonl` (`--root` defaults to `.sdd/audit`) and
verifies it hop by hop. Each receipt line binds `{run_id, hop_index, issuer,
subject, audience, act, created, prev_hmac, hmac}`; the HMAC covers the
previous receipt's HMAC concatenated with the canonical JSON of the current
receipt's body, using the install-scoped audit key
(`load_or_create_audit_key`) — the same construction as the main HMAC audit
chain.

Human output prints one line per hop (`issuer -> audience (act)`) followed by
a pass/fail summary. `--json` emits:

```json
{"run": "...", "valid": true, "incomplete": false, "hops": 2, "errors": [], "receipts": [
  {"hop_index": 0, "issuer": "...", "subject": "...", "audience": "...", "act": "..."}
], "verdict": {"verdict": "pass", "ok": true, "unproven_hops": 0, "reasons": [],
  "hops": [{"hop_index": 0, "verdict": "pass", "is_root": true,
    "reasons": ["root_structural_only"], "axes": [], "diagnostics": [],
    "parent_hop_index": null, "ancestor_hop_index": null, "principal": "..."}]}}
```

Exit codes: `0` when the chain matches the manifest-declared final head/count
and narrowing was checked and held;
`1` when there are zero receipts for the run, or any hop fails (HMAC mismatch
from a tampered field, a deleted hop, the wrong key, or a hop that widened its
parent's grant), the declaration or its audit-key seal is malformed, missing from a declared
head, or invalid, or the final digest/count disagrees; `3` when no final head was declared (`incomplete: true`,
`valid: false`), or the receipts carry too little to decide whether authority narrowed. `2` is left to click for usage errors.

**Compatibility note (issue #2554).** A chain that records no effective scope
at any hop used to exit `0`, which read the same as a chain whose narrowing was
checked and held. It now exits `3`, unproven. Nothing that exited `1` before
exits `0` now; the only changed outcome is `0` to `3`. Automation that treated
every valid legacy chain as success needs to decide whether unproven is
acceptable for its purpose, which is the question the old exit code hid.
A caller that wants each status handled explicitly:

```sh
run="$1"
ledger_root="${2:-.sdd/audit}"
status=0
bernstein delegation verify "$run" --root "$ledger_root" || status=$?
case "$status" in
  0) echo "narrowing checked and held" ;;
  1) echo "chain invalid or a hop widened" >&2; exit 1 ;;
  2) echo "usage error" >&2; exit 2 ;;
  3) echo "unproven: no narrowing was established, decide by policy" >&2; exit 3 ;;
  *) echo "unexpected exit status $status" >&2; exit "$status" ;;
esac
```

`--json` carries the same reading under `verdict`: a `verdict` of `pass`,
`fail`, or `unproven`, an `unproven_hops` count, and one row per hop with its
own verdict, reasons, and named scope axes. The reason strings are a closed
set, so a consumer can fail closed on exact matches.

What a pass does not establish: that runtime enforcement matched the recorded
scope, including consumption state such as remaining uses; that any grant was
appropriate policy; that no earlier authentic snapshot was replayed, or that no
alternate delegation path exists; that an unresolved reference would have
matched; anything about execution outcomes. unproven is not a pass, and pass is
the only positive claim.

Receipts are written by calling `DelegationLedger.record_hop` (or its
convenience wrapper `record_delegation_hop`) for each
`principal -> orchestrator -> sub-agent` handoff — there is no
`delegation emit` CLI verb; an operator only verifies, never hand-authors a
receipt. The write path runs at identity minting. When a run starts the
orchestrator mints one parentless run-root identity, records its id in the run
manifest (`.sdd/runtime/manifests/<run-id>.json`, field `run_root_identity_id`),
and every top-level agent is minted under it; a nested spawn is minted under its
spawning agent instead, from `AgentSession.parent_id`. Each parented mint records
one hop, issuer the parent and subject the child, carrying the child's task and
file scope on the receipt so narrowing is recomputable from the chain alone. An
parentless mint records nothing, and an empty chain is not a broken chain.

`delegation verify <run>` reads `run_root_identity_id` from the run manifest and
treats hops issued by that identity as chain roots, so several agents spawned by
the same run each grade instead of the second and later ones reading as
`root_claimed_mid_chain`. The name arrives from outside the receipts, so a
receipt still cannot promote itself out of a narrowing check by writing its own
issuer field, and a run whose manifest declares no root keeps the positional root
rule unchanged. The identity store is never consulted.

The write is fail closed at the spawn. A hop that cannot be written raises
`DelegationWriteError`, the spawner re-raises that type rather than starting the
agent, and a `spawn.refused_unreceipted` audit event records the run, the refused
session and the reason. Every other identity failure keeps its previous
log-and-continue behaviour, so a token that cannot be issued still does not stop
a run.

At run close, after the agent/executor drain and cleanup, the orchestrator
commits `delegation_head_hmac`, `delegation_hop_count` and `delegation_seal` to
the existing run manifest. The seal is HMAC-SHA256 under the install audit key,
with a `bernstein.delegation.manifest.v1` domain prefix and canonical JSON of all
manifest fields except `manifest_hash` and `delegation_seal`. It binds the run
ID, head, count, run-root identity and configuration. The public `manifest_hash`
covers the resulting fields too, but is a checksum, not authentication. The
startup checksum is checked before finalization; it cannot authenticate changes
made before sealing by a writer who can also recompute that checksum.

Tail recovery and atomic manifest replacement share the per-run append lock;
readers see either the startup manifest or the complete declaration. A crash
before close or a failed write leaves the run incomplete. Repeating close
requires the same tail/count and a valid existing seal; it cannot bless a
shorter or longer chain or silently upgrade an unsigned declaration.

Crew trace imports also commit a sealed final head and count after recording all
validated handoffs, under the same per-run append lock. Their declaration is
derived from the complete input trace. Re-importing the same trace is idempotent;
a different final head or count cannot replace an existing declaration.

Every run-delegation verifier reads the manifest and compares the authenticated
head/count with the reconstructed chain, detecting tail deletion, substitution
with another validly keyed receipt, post-close appends and interior deletion.
A keyless rewrite of both head/count and the public checksum fails the seal
check. Altering other sealed manifest fields, including the run-root identity,
also fails. Missing manifests and manifests without a final declaration are
incomplete, never valid. A declaration without a valid seal fails verification.

For incomplete runs, `chain_ok` reports only the integrity of the available
sequence and `authority` reports scope checks on those receipts; neither proves
completeness. The overall `verdict` is `unproven` (or `fail` if a check failed),
never `pass`. Access reviews mark incomplete and mismatched chains unverified.
The agent registry retains intact, authority-checked scopes from open and legacy
runs, reporting their IDs in `incomplete_runs`. This is an inventory of observed
authority, not a claim that all delegations are present. Pre-upgrade version-1
registry snapshots without this marker still verify against all original fields.

When exporting with `--root X`, preserve this exact relative layout:
`X/delegation/<run>.jsonl` and `X/../runtime/manifests/<run>.json`. Verify with the
original install audit key. The HMAC is shared-key authentication, not a
publicly verifiable signature: a holder of that key can forge both artifacts.
Deleting the declaration yields incomplete, never a verified complete chain.
Replaying an earlier authentic manifest and its matching ledger is not detected;
freshness needs an independently retained latest head. The seal also does not
prove that every runtime action was recorded or that imported traces were honest.

Runs closed before this change have no authenticated final declaration. There
is no automatic backfill: sealing whatever receipts remain would not establish
their historical completeness. Retain those runs as incomplete; a fresh run or
independently authenticated historical evidence is needed for a completeness claim.

## `bernstein delegation verify-token` — the capability-token chain

```
bernstein delegation verify-token TOKEN_FILE [--trust-anchor FILE ...] [--json]
```

Verifies a signed `CapabilityChain` read from `TOKEN_FILE`, fully offline.
For every hop it checks:

- the Ed25519 signature (detached JWS, RFC 7515 §A.5, over JCS-canonical
  bytes) against the `issuer_pubkey` and `subject_pubkey` captured at mint
  time — key rotation after minting never invalidates a historical token;
- structural linkage (`parent_token_hash`) and identity/pubkey continuity
  (the issuer of hop N must equal the subject of hop N-1);
- monotonic attenuation — every caveat (`permissions`, `task_ids`,
  `path_prefixes`, `not_after`, `max_uses`, `remaining_depth`) must narrow
  or stay equal at each hop, never widen;
- root trust-anchor membership.

`--trust-anchor` is repeatable and takes PEM public-key files; with none
given, the command falls back to the local agent-card keystore's public key
as the default anchor. If that keystore is missing or unreadable, the anchor
set is empty and the root-anchor check fails loudly rather than trusting an
unknown root.

Human output prints one `PASS`/`FAIL` line per hop plus the resolved
authority path (`issuer -> issuer -> ... -> leaf`). `--json` emits:

```json
{"valid": true, "principal_path": ["...", "..."], "hops": [
  {"hop_index": 0, "issuer": "...", "subject": "...", "ok": true, "errors": []}
]}
```

Exit codes: `0` only when every hop verifies; `1` on any failing hop, an
empty chain, or an unreadable/malformed token file.

## What this does not cover

- **Native subagent delegation** (`core/agents/subagent_delegation.py`,
  Claude Code's `--agents` flag and similar per-adapter mechanisms) has no
  dedicated CLI verb — it is an internal API called by the scheduler's
  dispatch path. Verifying a run's subagent-delegation boundaries means
  replaying its journal (see
  [Deterministic replay](deterministic-replay.md)) and, where an audit chain
  was wired in, checking `bernstein audit verify` for the mirrored
  `subagent.delegation` events. See
  [Native subagent delegation](../architecture/subagent-delegation.md).
- `bernstein delegation verify <run>` and `bernstein delegation verify-token`
  cover different surfaces on purpose — do not read a passing `verify-token`
  result as proof of the receipt chain, or vice versa.

## Source

`src/bernstein/cli/commands/delegation_cmd.py` (CLI),
`src/bernstein/core/identity/delegation.py` (per-hop receipt ledger),
`src/bernstein/core/security/capability_tokens.py` (attenuated capability
tokens). See also
[Security & identity stack](security-and-identity.md) for the token
attenuation model in depth.
