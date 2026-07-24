# Delegation chain verification

A run that fans out work carries delegated authority down the chain:
`principal -> orchestrator -> sub-agent`. Bernstein records two independent,
offline-verifiable trails of that authority — a per-hop receipt ledger (the
"ACT log": which principal authorized which sub-agent action) and a signed
capability-token chain (scoped, attenuating authority grants). `bernstein
delegation verify` and `bernstein delegation verify-token` check each trail
with no network call, no live coordinator, and no registry lookup.

The two surfaces are deliberately not coupled. A receipt may later reference
a token hash, but each verifies from its own bytes alone.

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
{"run": "...", "valid": true, "hops": 3, "errors": [], "receipts": [
  {"hop_index": 0, "issuer": "...", "subject": "...", "audience": "...", "act": "..."}
]}
```

Exit codes: `0` when the chain is intact (at least one hop, every hop
verifies from genesis to tail); `1` when there are zero receipts for the run,
or any hop fails (HMAC mismatch from a tampered field, a deleted hop, or the
wrong key).

Receipts are written by calling `DelegationLedger.record_hop` (or its
convenience wrapper `record_delegation_hop`) for each
`principal -> orchestrator -> sub-agent` handoff — there is no
`delegation emit` CLI verb; an operator only verifies, never hand-authors a
receipt. **Limitation:** as of this writing, no orchestration code path in
this codebase calls `record_delegation_hop`; the ledger and its verifier are
exercised directly by unit tests. Until a caller wires the write path into a
real run, `bernstein delegation verify <run>` against a live run's default
root correctly reports "no receipts" (exit 1) rather than a populated chain.

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
