# ADR-009: Lineage v1 - Sigstore-style per-artefact transparency log

**Status**: Accepted
**Date**: 2026-05-13
**Context**: Bernstein multi-agent orchestration system

---

## TL;DR

✅ **Problem.** Two agents in separate worktrees touch the same artefact. Last writer wins silently. No record of parallel edits. Brittle.

✅ **Solution shape.** Append-only content-addressed lineage log per artefact + Ed25519-signed entries + Sigstore-style transparency model. Compatible with A2A v1.0, MCP, EU AI Act Article 12.

✅ **Operator surfaces.** `bernstein compliance pack` (one-command Article 12 evidence bundle) + `bernstein-verify` (standalone auditor CLI, no Bernstein install needed) + 3 reference demos (fintech / healthcare / EU manufacturer).

⏳ **Build mode.** 5 parallel worktree agents under one steward branch `feat/lineage-v1`. Wall-clock target: 3-4 days dispatch + integration.

---

## 1. Scope

### 1.1 What lineage covers

| Artefact class | In scope | Why |
|---|---|---|
| Source files (git-tracked) | ✅ | Core case |
| `.sdd/runtime/*` task specs, briefs, scratch | ✅ | Inter-agent shared state |
| MCP tool results persisted to disk | ✅ | Memory-poisoning surface (OWASP ASI06) |
| Generated config (e.g. agent cards, registry diffs) | ✅ | Privilege-escalation surface (OWASP ASI03) |
| Mem0 / Zep / Graphiti entries | ❌ | Different store; defer to those systems' own audit |
| Transient stdout / log lines | ❌ | Existing audit log handles |
| `.git/` internals | ❌ | Git's own object DB |

### 1.2 What lineage is NOT

- Not a replacement for `audit.jsonl`. Audit logs every tool call; lineage logs every artefact write. They cross-link via `tool_call_id`.
- Not a memory store. Doesn't recall content; recalls **provenance of content**.
- Not a CRDT. Doesn't auto-merge concurrent writes - surfaces them as siblings for Steward.

---

## 2. Architecture

### 2.1 High-level flow

```
Agent (in worktree)
  │
  │ writes file foo.py via Bernstein adapter
  ▼
LineageRecorder.record_write(artefact_path, new_content)
  │
  │ ① compute content_hash = sha256(new_content)
  │ ② look up current tip(s) of foo.py from tips/<hash-of-path>.json
  │ ③ build entry: {artefact_path, content_hash, parent_hashes: [tip], agent_id, ...}
  │ ④ JCS-canonicalize entry (RFC 8785)
  │ ⑤ sign canonical bytes with agent's Ed25519 key (JWS RFC 7515 detached)
  │ ⑥ wrap in HMAC envelope (existing pattern)
  │ ⑦ append to log.jsonl
  │ ⑧ update by-artefact/<hash[:2]>/<hash>.jsonl projection
  │ ⑨ update tips/<hash-of-path>.json
  │
  ▼
OTel span emitted; cross-links to span_id in audit.jsonl
```

### 2.2 Components (one per worktree agent)

| Component | Module | Lines (target) | Owner agent |
|---|---|---|---|
| `LineageRecorder` | `core/lineage/recorder.py` | ~300 | A |
| Entry schema + JCS canonical | `core/lineage/entry.py` | ~150 | A |
| Storage / log writer | `core/lineage/store.py` | ~250 | A |
| Conflict detector + CI gate | `core/lineage/gate.py` | ~200 | B |
| `bernstein lineage` CLI subcommand | `cli/commands/lineage_cmd.py` | ~200 | B |
| `bernstein compliance pack` | `core/compliance/pack.py` | ~400 | C |
| Article 12 evidence renderer (PDF + CSV + bundle) | `core/compliance/article12.py` | ~300 | C |
| `bernstein-verify` standalone CLI (separate console_script) | `cli/verify_main.py` | ~250 | D |
| MCP `lineage://artefact/<p>` resource | `mcp/resources/lineage.py` | ~150 | A |
| 3 demo scenarios under `examples/lineage/` | `examples/lineage/{fintech,healthcare,eu-mfg}/` | ~200 each | E |

Total: ~2,600 LOC across 5 agents.

### 2.3 Reuses existing infra (no rewriting)

| Existing module | What we use | Touched? |
|---|---|---|
| `core/security/lineage_kms.py` (432 LOC) | KMS-backed key storage | Read |
| `core/security/audit_head_signature.py` (255 LOC) | HMAC head signing pattern | Read |
| `core/persistence/merkle.py` | Merkle tree primitive | Read (verify chain) |
| `core/agents/agent_identity.py` (862 LOC) | Ed25519 keypair per agent | Read + extend Agent Card emit |
| `core/security/audit.py` | HMAC envelope | Wrap |
| `core/observability/lineage_alert.py` (171 LOC) | Alert routing | Wire conflict events |
| `core/git/commit_provenance.py` (356 LOC) | Trailer format | Cross-reference |

---

## 3. Data model

### 3.1 Lineage entry schema (v1, JCS-canonical JSON)

```jsonc
{
  "v": 1,
  "artefact_path": "src/bernstein/cli/main.py",     // repo-relative POSIX
  "artefact_kind": "file",                           // file | sdd-runtime | mcp-result | config
  "content_hash": "sha256:a1b2c3...",                // sha256 of artefact bytes after write
  "parent_hashes": ["sha256:01af..."],               // 0 = genesis, 1 = normal, ≥2 = merge
  "agent_id": "agent:claude-worker-3",               // Bernstein-issued agent slug
  "agent_card_kid": "key-2026-05-13-001",            // A2A Agent Card key id
  "tool_call_id": "tc-7f3a8b9c",                     // cross-link to audit.jsonl
  "span_id": "00f067aa0ba902b7",                     // OTel span hex
  "ts_ns": 1715600000000000000,                      // ns since epoch
  "operator_hmac": "deadbeef..."                     // existing HMAC envelope sig (operator secret)
}
```

### 3.2 Detached signature (sidecar, NOT in canonical body)

```jsonc
{
  "entry_hash": "sha256:...",                        // sha256 over JCS-canonical entry above
  "jws": "eyJhbGc...",                                // Ed25519 JWS over entry_hash (RFC 7515 detached)
  "agent_card_url": ".sdd/agents/<agent-id>/card.json"
}
```

### 3.3 Why split entry vs signature

- Entry is HMAC-protected via operator secret (Bernstein-internal tamper detection).
- Signature is Ed25519 (asymmetric, third-party verifiable without operator secret).
- An **external auditor** verifies the JWS with the publicly-fetched Agent Card; they don't need the operator HMAC key.
- This is the Sigstore play: the entry is the log line, the signature is the verifiable provenance.

---

## 4. Storage layout

```
.sdd/lineage/
├── log.jsonl                              # append-only, single source of truth
├── log.jsonl.head-sig                     # rolling HMAC head signature (existing pattern)
├── by-artefact/
│   └── a1/                                # first 2 chars of sha256(artefact_path)
│       └── a1b2c3d4...e5f6.jsonl          # full hash → projection of log.jsonl entries for that path
├── tips/
│   └── a1b2c3d4...e5f6.json               # {"open":[<entry_hash>...], "merged":[<entry_hash>...]}
└── signatures/
    └── a1/                                # mirrors by-artefact sharding
        └── a1b2c3d4...e5f6/<entry_hash>.jws
```

**Invariants**:
- `log.jsonl` is the source of truth. Everything else is a projection rebuildable via `bernstein lineage reindex`.
- `tips/<hash>.json.open` has exactly 1 element in steady state; >1 = unresolved fork.
- Signatures are per-entry files (not appended to log) - auditor downloads only entries they care about.

---

## 5. Identity + signing

### 5.1 Agent Card emission

When `bernstein conduct` spawns an agent:
1. Generate Ed25519 keypair (or reuse from `agent_identity.py`).
2. Emit Agent Card at `.sdd/agents/<agent-id>/card.json` following A2A v1.0 spec (`protocolVersion`, `name`, `url`, `capabilities`, `signatures[]`).
3. Card itself is self-signed (RFC 7515 + RFC 8785 - JWS over JCS-canonical card body). **Bernstein default = Ed25519** across the lineage layer (entry signatures, card signatures). ES256 accepted on external (federated) Agent Cards per A2A spec but never emitted by us.
4. Public key embedded in card; private key in `.sdd/agents/<agent-id>/key.pem` (chmod 600).

### 5.2 Signing flow per write

1. Recorder builds entry (without signature).
2. Canonicalize per RFC 8785 (JCS).
3. `entry_hash = sha256(canonical_bytes)`.
4. `jws = Ed25519-JWS-detached(entry_hash, key=agent.private_key, kid=agent.card.kid)`.
5. Write entry to `log.jsonl`.
6. Write jws to `signatures/.../<entry_hash>.jws`.

### 5.3 Verification

External auditor (with `bernstein-verify`):
1. Reads `log.jsonl` as bytes; splits strictly on `\n`.
2. For each entry: require the on-disk line to equal `canonicalise(entry)`
   byte-for-byte (reject non-canonical bytes), then recompute `entry_hash`
   from the canonical bytes. (Compliance-pack logs follow the same rule for
   format v2; see §8.4 for the v1 fallback.)
3. Fetches Agent Card from `.sdd/agents/<agent-id>/card.json` or signed registry.
4. Verifies JWS using card's public key.
5. Walks `parent_hashes` chain back to genesis.
6. Verifies tips: every artefact must have exactly one open tip OR a merge entry resolving prior forks.

---

## 6. Conflict detection + CI gate

### 6.1 Fork detection algorithm

For each artefact_path:
1. Group entries by `parent_hashes`.
2. If 2+ entries share the same single parent_hash with **different** content_hash → **fork**.
3. Resolved if a later entry has `parent_hashes = [child1_hash, child2_hash]` (Steward merge).
4. Unresolved fork at PR submission time → CI gate FAIL.

### 6.2 CI gate (fitness function)

New workflow check: `Lineage Gate`. Required for merge to main.

```python
# scripts/check_lineage.py (called from .github/workflows/ci.yml)
def main():
    log = read_jsonl(".sdd/lineage/log.jsonl")
    by_path = group_by(log, key="artefact_path")
    failures = []
    for path, entries in by_path.items():
        tips = compute_tips(entries)
        if len(tips["open"]) > 1:
            failures.append(f"{path}: {len(tips['open'])} unresolved tips: {tips['open']}")
        for entry in entries:
            if not verify_jws(entry):
                failures.append(f"{path}: invalid signature on entry {entry['entry_hash']}")
            if not verify_hmac(entry):
                failures.append(f"{path}: HMAC mismatch on entry {entry['entry_hash']}")
    if failures:
        print("Lineage gate failed:\n" + "\n".join(failures))
        sys.exit(1)
```

### 6.3 Steward responsibility

When Steward merges N agent branches:
1. For each artefact touched in >1 branch: read all open tips.
2. Resolve content using a **conflict-resolution policy** (lookup order):
   - `bernstein.lineage.merge_policy = "human"` (v1 default) - emit a `LineageConflict` event; block until operator runs `bernstein lineage merge <path>` (interactive prompt or accepts `--use-content <hash>`).
   - `bernstein.lineage.merge_policy = "first-writer"` - pick the entry with the earliest `ts_ns`; tiebreak by `agent_id` lex order.
   - `bernstein.lineage.merge_policy = "agent:<id>"` - designated agent's tip always wins (e.g. dedicated reviewer agent).
3. Write merge entry: `parent_hashes = [tip1, tip2, ...]`, `content_hash = sha256(resolved_content)`, `merge_policy_used = "human"`.
4. Steward signs with its own Agent Card key (Ed25519).
5. CI gate now passes.

Steward privilege is enforced by **policy** (allowlist of agent_ids permitted to write merge entries), not by signature shape. Same key type as workers.

---

## 7. MCP exposure

### 7.1 Resources

- `lineage://artefact/<repo-relative-path>` → returns full chain as JSONL
- `lineage://stats` → counts: total entries, open forks, agents seen, last 24h activity

### 7.2 Tools

- `record_lineage_event(path, content_hash, parent_hashes, ...)` - for agents writing through non-adapter paths
- `verify_chain(path)` - returns ok/err + reason

### 7.3 Default off in untrusted contexts

MCP exposure is gated by `bernstein.lineage.mcp.enabled` config (default `false` for remote MCP, `true` for local stdio).

---

## 8. Compliance pack

### 8.1 Command

```bash
bernstein compliance pack --since 2026-01-01 --until 2026-05-13 \
  --org "Acme Corp" --output ./acme-compliance-2026-q2.zip
```

### 8.2 Bundle contents (the ZIP)

```
acme-compliance-2026-q2/
├── README.md                              # cover page; what's in here
├── article12-evidence.pdf                 # human-readable EU AI Act §12 summary
├── article12-evidence.csv                 # machine-readable: every artefact write, agent, ts, content hash
├── lineage-log.jsonl                      # raw log for re-verification
├── signatures/                            # per-entry detached JWS sigs
├── agent-cards/                           # all Agent Cards seen during period
├── verify-instructions.md                 # how to run bernstein-verify against this bundle
├── pack-manifest.json                     # SLSA-style provenance: who packed, when, hashes
└── pack-manifest.json.sig                 # Operator-signed manifest (Ed25519, operator KMS key)
```

### 8.3 Why this matters

A compliance officer at a regulated company can:
1. Receive the ZIP from their engineering team.
2. Run `bernstein-verify pack ./acme-compliance-2026-q2.zip` on their air-gapped laptop.
3. Get a one-line PASS/FAIL plus a structured report mapped to Article 12 paragraph numbers.

The pack is the artifact that makes lineage independently checkable: without it, verification requires access to the producing repository.

### 8.4 Pack format versions and on-disk byte binding (#1871)

`pack-manifest.json` records `pack_format_version`. The offline auditor
(`bernstein-verify pack`) dispatches its log-parsing rule on it:

| Version | `lineage-log.jsonl` on disk | Verification rule |
|---|---|---|
| **v2** (current) | Each entry written in its exact RFC 8785 canonical bytes, one per line, single trailing `\n`. | Bound to the on-disk bytes: split strictly on `\n`, every line must equal `canonicalise(entry)` byte-for-byte, a missing trailing newline is tamper-evidence. |
| **v1** (pre-fix) / no manifest | Each entry written with `json.dumps(..., sort_keys=True)` default separators (spaced `", "` / `": "`), so the bytes are **not** canonical. | Original rule: re-canonicalise the parsed entry and verify the JWS against that re-derived form. |

**Why the split exists.** v1 packs re-canonicalised the parsed entry before
checking the signature, so any value-preserving byte rewrite - reordered JSON
keys, inserted whitespace, a flipped or stripped line terminator - parsed to
identical fields, re-canonicalised to identical bytes, and verified as
authentic. v2 binds verification to the exact stored bytes (matching the
in-tree lineage gate, §4 / #1848), so such a rewrite is rejected.

**Legacy default.** An absent manifest or an absent/unparseable
`pack_format_version` is treated as v1 (re-canonicalise rule), mirroring the
Merkle seal's scheme dispatch (#1866) so pre-fix packs still verify. The
version field rides inside the operator-signed manifest body, so a downgrade
that rewrites it to escape the v2 rule invalidates `pack-manifest.json.sig` -
the signature an operator checks on the with-key path.

**Operator action (one-time re-pack).** Packs built before this change are
v1 and verify under the weaker re-canonicalise rule. To get byte-level tamper
protection for an existing window, re-run `bernstein compliance pack` for that
`(since, until, org)` triple; the regenerated pack is v2. Existing archived v1
packs remain verifiable - no forced break.

---

## 9. `bernstein-verify` - auditor CLI

### 9.1 Why separate

- Bundled in its **own wheel** (`bernstein-verify` on PyPI).
- Pure-stdlib (no `bernstein` install required).
- Auditor's laptop may not have Python frameworks; we keep it minimal.

### 9.2 Commands

```bash
bernstein-verify chain <path> [--lineage-dir DIR]    # verify single artefact chain
bernstein-verify pack <bundle.zip>                    # verify compliance pack end-to-end
bernstein-verify forks <path> [--lineage-dir DIR]     # report unresolved forks (CI use)
```

### 9.3 Output

- Exit 0 = on-disk log bytes are canonical (format v2) + all signatures valid + chains complete + no unresolved forks.
- Exit 1 = any failure; structured JSON to stderr, human summary to stdout. A non-canonical line in a v2 pack is reported as `non-canonical line bytes`; a stripped terminator as `missing trailing newline`.

---

## 10. Three demo scenarios

Each lives under `examples/lineage/<demo>/` with: README, mock `.sdd/lineage/` state, sample `compliance pack` output, and a `make demo` target.

### 10.1 Fintech: SOC2 + Bernstein

- Story: 4 agents (audit-helper, code-reviewer, security-scanner, docs-bot) edit a payment-flow file over a 2-week period.
- Compliance pack shows: every change, every agent identity, every signature, no unresolved forks.
- Bonus: a deliberate "rogue agent" attempt detected by `bernstein-verify forks`.

### 10.2 Healthcare: HIPAA + EU AI Act

- Story: An AI agent writes a triage decision-support config. Article 11 (technical docs) + Article 12 (event log) requirements explicit.
- Compliance pack maps every Article 12 paragraph to specific log entries.

### 10.3 EU manufacturer: high-risk AI Act Annex III

- Story: Industrial automation agent updates a safety threshold config. EU AI Act high-risk classification.
- Demo shows: 10-year retention via cold storage; chain reverification after restoring from cold storage.

---

## 11. Migration / bootstrap

### 11.1 Existing repos

- New install of Bernstein with lineage v1: existing files have NO lineage entries.
- First time an agent writes an existing file: write a **genesis** entry (`parent_hashes: []`) with content_hash of the file BEFORE the agent's write, AND a child entry with content_hash AFTER. This anchors history at the moment lineage went live.
- No retroactive history reconstruction. We never claim to know who wrote pre-existing files.

### 11.2 Feature flag

- `bernstein.lineage.enabled` (default `true` after this PR ships).
- `bernstein.lineage.strict` (default `false` for first release, then `true`): when `true`, agent writes are REJECTED if lineage recording fails. When `false`, lineage failures log warnings but writes proceed.
- One release (1.10.9 lineage-soft) → 30 days observation → 1.11.0 lineage-strict.

### 11.3 Backward compat

- `commit_provenance.py` trailer format: kept, deprecated in 1.12, removed in 2.0.
- Existing `audit.jsonl`: unchanged. Lineage cross-links via `tool_call_id`.

---

## 12. Testing strategy

### 12.1 Unit tests (per component)

| Component | Coverage target | Notable cases |
|---|---|---|
| Entry canonicalization | 95% | RFC 8785 conformance suite, Unicode normalization edge cases |
| JWS detached sign/verify | 95% | RFC 7515 conformance, key rotation, expired keys |
| Storage / log writer | 90% | Concurrent writes (fsync, lock semantics), torn writes |
| Conflict detector | 95% | All fork shapes: simple, diamond, criss-cross, N-way |
| CI gate | 95% | Exit codes, JSON output, missing files |
| Compliance pack | 90% | ZIP integrity, PDF rendering, CSV escaping |
| bernstein-verify | 95% | Air-gap (no network), no-bernstein install, garbage input |

### 12.2 Property-based tests (Hypothesis)

These run as `tests/property/test_lineage_properties.py`:

1. **Chain integrity invariant**: For any sequence of writes, every entry's parent_hashes point to entries that exist in the log.
2. **Fork detection completeness**: If 2 entries share parent + differ in content, `compute_tips` reports them as open.
3. **Merge resolution**: After a merge entry pointing to N forks, `compute_tips` reports those N as merged.
4. **Signature roundtrip**: For any well-formed entry + key, JWS sign followed by verify always returns true; tampering with any byte fails verify.
5. **JCS determinism**: For any input dict, canonical bytes are deterministic regardless of key order.
6. **HMAC envelope tamper detection**: Flip any byte in entry → HMAC verify fails.

### 12.3 Mutation testing (`mutmut`)

Run on:
- `core/lineage/recorder.py`
- `core/lineage/gate.py`
- `core/lineage/store.py`
- `cli/verify_main.py`

Threshold: ≥75% mutation kill rate on these critical modules.

### 12.4 E2E tests

`tests/integration/test_lineage_e2e.py`:

1. **Parallel agent fight**: Spin up 2 worktree agents, have both write `same_file.py`, run CI gate → must FAIL with fork report. Have Steward write merge entry → CI gate now passes.
2. **Compliance pack roundtrip**: Generate pack, unzip, run `bernstein-verify pack` → exit 0.
3. **Auditor flow without bernstein installed**: In a clean venv with ONLY `bernstein-verify` installed, verify a pack generated by full Bernstein.
4. **Air-gap verify**: bernstein-verify with no network access - must succeed (no remote lookups).
5. **Tamper detection**: Flip a byte in `log.jsonl` → both `bernstein-verify chain` and `bernstein lineage gate` fail with specific error.
6. **Chain replay against rebuilt indices**: Delete `by-artefact/` and `tips/`, run `bernstein lineage reindex`, verify state matches the pre-deletion state.

### 12.5 Performance / load

`tests/perf/test_lineage_perf.py` (smoke, not in required CI):

- 10,000 writes across 100 artefacts: assert <30s on commodity hardware.
- Compliance pack for 10,000 entries: assert <10s to generate.
- bernstein-verify pack on 10,000 entries: assert <5s.

### 12.6 Adversarial test (the "skeptical" part)

`tests/security/test_lineage_adversarial.py`:

1. **Replay attack**: replay an old entry into a new run → must be rejected (entry has run-id binding via span_id).
2. **Substitution attack**: swap two entries' signatures → verify fails.
3. **Privilege escalation**: worker agent writes a merge entry → CI policy rejects.
4. **Forge attack**: synthetic JWS with fake kid → verify fails (kid not in known Agent Cards).
5. **Path traversal in artefact_path**: `../../../etc/passwd` → recorder rejects.

---

## 13. Build sequence (parallel agents)

Each agent runs in its own `git worktree` under `feat/lineage-v1` umbrella branch.

```
feat/lineage-v1                          (steward integration branch)
├── feat/lineage-v1-core (agent A)       core recorder + entry + store + MCP resource
├── feat/lineage-v1-gate (agent B)       conflict detector + CI gate + lineage_cmd
├── feat/lineage-v1-compliance (agent C) pack + Article 12 renderer
├── feat/lineage-v1-verify (agent D)     standalone bernstein-verify CLI
└── feat/lineage-v1-demo (agent E)       3 demo scenarios + docs
```

### 13.1 Phase 0 - schema lock (no parallelism)

Steward writes `src/bernstein/core/lineage/entry.py` first (just the schema + JCS canonical + dataclasses). Pushed to `feat/lineage-v1`. All other agents branch off THIS.

### 13.2 Phase 1 - parallel fan-out (A, B, C, D, E all simultaneously)

Each agent gets a focused prompt + the schema file + this design doc. Agents are dispatched in parallel worktrees.

### 13.3 Phase 2 - Steward merge

Steward rebases each branch onto `feat/lineage-v1`, resolves conflicts, runs full test suite locally, opens one PR.

### 13.4 Phase 3 - CI + release

PR runs full CI matrix (we already have it from bughunt). Once green: merge → version bump → auto-release.

---

## 14. Risks + mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| EU AI Act Article 12 enforcement timeline shifts | Medium | Compliance-driven adoption arrives later | The evidence format is standards-based (RFC 7515/8785, SLSA-style manifest), so the same bundle serves SOC 2 and ISO 42001 audits |
| Sigstore-style is too complex to operate | Medium | Adoption stalls | The `compliance pack` is the simple surface; complexity hidden |
| Performance regression: 10k writes too slow | Low | UX issue | Perf tests in §12.5; batch fsync; async store flush |
| Operator key rotation breaks chain | Low | Audit-chain HMAC mismatches (we've seen this) | Document rotation procedure; ship `bernstein lineage rotate-hmac` helper |
| Steward becomes single point of failure | Low | Merges blocked | Multiple Steward agents allowed; policy allowlist |

---

## 15. Acceptance criteria

- ✅ All tests pass (unit + property + mutation ≥75% + e2e + adversarial).
- ✅ CI gate runs in <30s on typical PR.
- ✅ Compliance pack for 10k entries generates in <10s.
- ✅ bernstein-verify works in air-gap, no-bernstein-install scenario.

---

## 16. Open questions (carry into writing-plans)

1. **Operator HMAC vs Ed25519 dual-signing**: do we need BOTH? Decision deferred - start with both, can drop HMAC if Ed25519 alone passes operator security review.
2. **Agent Card registry**: local-only files or also a signed remote registry? Start local; remote in v1.1.
3. **Cold-storage / Article 11 10-year retention**: who packages it? Out of scope for v1; document the export path.
4. **MCP authentication for lineage tools**: OAuth 2.1 per MCP June 2025 spec, or local-only? Local-only for v1.

---

## 17. References

- RFC 7515 JWS, RFC 7517 JWK, RFC 8785 JCS
- A2A v1.0 Agent Card spec
- Sigstore Rekor transparency log
- SLSA v1.1 provenance
- EU AI Act Articles 11, 12, 14
- OWASP Top 10 for Agentic Apps: ASI03, ASI06, ASI07
