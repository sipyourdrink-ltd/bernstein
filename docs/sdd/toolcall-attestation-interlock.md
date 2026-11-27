# Tool-call attestation interlock

## Scope

The interlock is the orchestrator-owned boundary between evidence preparation
and a live connector side effect. It does not replace
`RunInstrumenter.log_tool_call`: instrumentation remains observe-only and may
never take a run down. The interlock is opt-in and applies only to calls that
cross a host dispatch hook such as `MCPGateway`.

## Contract

`ToolCallAttestationInterlock` accepts a `ToolCallEvidenceProvider` and an
opaque `scope_id`. The scope binds the provider's run, agent identity, and
authority context without requiring Bernstein's gateway to understand the
provider's identity, key, or policy schema.

For each pending call, the gateway derives a `ToolCallIntent` from:

- the opaque scope;
- server and method;
- tool name and JSON-RPC request id;
- the content-derived request span;
- a SHA-256 digest of canonicalized arguments.

The provider must verify and durably record the call's attestation and a
dispatch marker that references it. It returns opaque attestation and dispatch
references plus the exact intent digest. The interlock validates the scope,
non-empty references, and intent-digest equality before upstream I/O becomes
reachable.

## Modes and invariants

### Enforced

Any provider, verification, durability, empty-handle, or intent-mismatch
failure raises `ToolCallInterlockError`. The connector is invoked zero times.

### Observed

The same failure is logged and dispatch may continue. Receipt projection
reports `observed`; a caller-supplied `complete` label has no authority.

### Chain-derived verdict

`complete` requires at least one `toolcall.enforced_dispatch` marker and a
preceding `toolcall.attestation` with the same attestation reference and intent
digest. Each attestation reference may authorize at most one dispatch. For an
identity-anchored run, every attestation must also carry a valid signed identity
envelope whose run, agent, scope, request, arguments, intent, monotonic call
index, frozen journal head, chain predecessor, anchor reference, key id, and
attestation time verify against the public key frozen at spawn. Missing,
reordered, duplicated, substituted, or mismatched evidence deterministically
downgrades to `observed`. Absence fails closed in both shapes: an attestation
attributed to a run whose anchor binds a tool signing key but carrying no
envelope downgrades, and an attestation that cannot be attributed to any
anchored run while a key-bearing identity anchor exists downgrades the same
way — emptying `run_id` is not an escape from the per-run rule. A run whose
anchor binds no tool key keeps its HMAC-only semantics: missing envelopes on
its attestations do not downgrade a verdict that the chain-derived walk would
otherwise report.

## Trust boundary

`NativeToolCallEvidenceProvider` is Bernstein's first implementation. It writes
the ordered `toolcall.attestation` and `toolcall.enforced_dispatch` records to
the existing HMAC audit chain while holding one cross-process chain
transaction. Raw tool arguments are never retained; only their canonical
SHA-256 digest is recorded. If the second append fails, no evidence handle is
returned, the connector remains unreachable in enforced mode, and the lone
attestation projects as `observed` rather than `complete`.

After the connector returns, the same provider appends `toolcall.effect`
bound to that intent digest (`record_toolcall_effect`, same shape as
`record_automation_action`: digest, dispatch handle, attestation handle,
outcome). Fields are `effect_digest`, `outcome` (`ok` / `error` / `timeout` /
`partial`), and `duration_ms`. A missing or failed effect write is
"dispatched, effect unobserved", never success. Completeness still comes
from the pre-dispatch pair only; slice 2 adds the verifier triple.
Production `bernstein gateway start` still constructs `MCPGateway`
without `attestation_interlock`: the native writer is opt-in, and slice 1
does not turn the CLI proxy into an always-on interlock.

Canonical form of `effect_digest`: SHA-256 of the connector's JSON-RPC
response line exactly as received, after stripping surrounding whitespace
and the line terminator (`_read_upstream_loop` keeps those bytes per request
id). Nothing is parsed and re-serialised, so key order, spacing, `\u`
escapes, timestamps and echoed request ids all count — a connector that
echoes a timestamp produces a different digest. A string-only `result`
(and an explicit patch) uses SHA-256 of the UTF-8 text instead, the same
preimage as `ResultBundle.patch_sha256`. A timeout hashes JSON `null`. A
response the host did not read off the wire (a direct `after_dispatch`
caller without `received`) falls back to compact JSON
(`separators=(",", ":")`, `ensure_ascii=False`, no `sort_keys`).

Verification path: no Bernstein record holds the received line today. The
effect event keeps only `effect_digest`, and the WAL keeps the parsed
`result` / `error`, which cannot reproduce the original bytes. Slice 1
therefore binds what the host saw. The digest can be checked only against an
independent capture of the connector's output, never recomputed from the run's
artifacts. Slice 2 must persist the received line (for example
content-addressed beside the WAL entry) before the verifier can recompute
`effect_digest` offline.

With a run identity, lineage signer, and journal-head reader configured, the
native provider additionally creates a versioned, JCS-canonicalized Ed25519
identity envelope before either marker is appended. Its signature input is
domain separated with `bernstein.toolcall.identity-attestation/v1`, so a valid
signature from another Bernstein evidence family cannot be replayed here. The
private key remains behind the narrow signer protocol and is never serialized.
The signed `attested_at_ns` comes from an injected clock, making replay tests
byte-deterministic.

Legacy HMAC-only construction remains supported. The identity extension does
**not** issue identities, evaluate policy, provide revocation, require hardware
keys, or introduce Nxtlinq (or any vendor) as a dependency. External providers
can implement the same interlock contract without becoming core dependencies.

This boundary does not contain direct filesystem, process, network, or
connector effects that bypass the host hook. A completeness statement is valid
only for the dispatch surfaces wired through the interlock.

## Source-rights evidence proposal (#6409)

This section is a proposal with no implementation. It describes evidence for
a local source-rights decision before dispatch; it does not extend the current
interlock contract, enable a hook, or select a provider. Field placements and
the evidence schema are not yet agreed.

Evidence grants no licence, proves neither ownership nor downstream use, and
does not replace access control. A provider's statement is an input to the
operator's policy decision, not a legal entitlement or proof of compliance.
Existing authentication, authorization and resource restrictions still apply.

### Entry point

The proposed entry point is the host boundary before an in-scope action is
dispatched. The resource and intended purpose must be declared explicitly;
neither a tool name nor the argument digest alone supplies that meaning.
`ToolCallIntent` currently carries call identifiers and an argument digest,
without dedicated resource or purpose evidence
(`src/bernstein/core/security/toolcall_interlock.py`, `ToolCallIntent`).

The following actions need separate declarations and decisions; permission
for one purpose must not be inferred to cover another:

- **fetch**: identify the source resource to retrieve, including its version
  or content identity when available, and the intended retrieval purpose,
  such as transient reading for a particular task.
- **ingest**: identify the source material or collection and its destination,
  and declare the intended copying, retention or transformation purpose.
- **index**: identify the source material and target index, and declare the
  intended indexing purpose, including the planned retrieval use.
- **search**: identify the collection or index being queried and the intended
  query and result-use purpose. Evidence for building an index does not by
  itself cover searching it or reusing its results.
- **training**: identify the source dataset or material and target training
  job, and declare the intended model-training or fine-tuning purpose.
  Retrieval or indexing evidence does not imply a training entitlement.

The operator would configure the source-rights evidence provider explicitly,
including its identity, lookup mechanism, trust configuration and the action
boundaries at which it is consulted. An agent's tool arguments must not choose
a different provider or trusted key. No provider is selected or configured
by this note, and no vendor dependency is proposed as a prerequisite.

For each decision, bind the evidence ID or hash to that specific decision
and the exact tool-call intent digest, together with the declared resource,
purpose and provider identity. Reusing a cached provider answer still needs
a separate binding to each decision and intent; a reference alone must not
become a reusable dispatch authorization.

One possible placement is a source-rights evidence reference in a decision
record, with resource and purpose declarations attached to the intent or an
associated evidence envelope. These are proposed field placements, not yet
agreed fields, APIs or enums. How those declarations become authenticated
parts of the binding remains an open design decision.

### Failure semantics

The proposed local policy permits only the following outcomes when usable
evidence cannot be established. Dispatch count refers to the blocked action,
not a provider lookup.

| condition | permitted local outcomes | dispatch count |
| --- | --- | --- |
| UNKNOWN | review/deny | 0 |
| stale | review/deny | 0 |
| unsigned | review/deny | 0 |
| unreachable | review/deny | 0 |

`UNKNOWN` means that evidence does not establish an answer for the declared
resource and purpose. `stale` means its freshness window has expired;
`unsigned` means it lacks the required signature. `unreachable` includes a
provider lookup that fails or exceeds its bounded timeout.

Record the reason and the chosen local outcome (`review` or `deny`) for each
decision, alongside its evidence and intent binding. `review` keeps the
action blocked until it is resolved; queuing a review is not permission to
dispatch. The resolution authority and procedure remain open decisions.

Two existing constraints prevent treating this proposal as current behavior:

- `ToolCallAttestationInterlock.before_dispatch` skips evidence-preparation
  errors in observed mode: it logs the failure and returns `None`, allowing
  dispatch to continue (`src/bernstein/core/security/toolcall_interlock.py`).
  Observed mode therefore cannot act as a mandatory source-rights block.
- `PolicyHookRegistry._record` records the engine's verdict rather than the
  registry's final decision
  (`src/bernstein/core/security/external_policy_hook.py`). For example, the
  audit can retain `UNAVAILABLE` when the registry resolves it to denial.
  That record alone does not establish the chosen local source-rights outcome.

Neither constraint is changed here. The failure table is a proposed
requirement, not a claim that the existing paths enforce it.

### Offline verification

Retain the signed data in its verifiable form, its digest, the signature and
the pinned trusted public key used for verification. Preserve the provider
identity, resource, purpose, freshness information and binding to the decision
and intent so a later verifier can check the same claim without contacting
the provider. Retain the signature and digest interpretation needed to
reproduce verification; their concrete formats remain undecided.

An evidence ID or hash alone is not enough: it cannot reconstruct the signed
data, signature or trusted key after the provider becomes unavailable.
Offline verification checks the retained statement and its binding; it does
not establish ownership, grant a licence or prove subsequent use.

### Latency and cache

Provider lookup must have a bounded timeout. A timeout leads to the
`unreachable` row above and never permits the pending action. The numeric
budget is an open decision; this note sets no runtime default.

Key the cache by resource, purpose and provider, retaining the resource's
version or content identity where declared. Record the fetch time, freshness
window and whether the answer was a cache hit in the decision evidence.
A cache hit must still satisfy signature, trust and decision-binding checks.

An expired entry never permits an action. A failed refresh cannot turn stale
evidence into permission: choose `review` or `deny` and keep dispatch at zero.
Bound cache size and retention as well as lookup time; the numeric limits
remain open. Caching does not remove the offline evidence-retention needs.

### Open decisions

- The decision-record field and evidence schema, including the placement and
  authentication of the evidence reference and its decision/intent binding.
- How resource and purpose are declared, their identity and granularity, and
  which actions and dispatch surfaces are in scope.
- Whether the default local outcome is `review` or `deny`, who may resolve a
  review, and how resolution is recorded before any later dispatch.
- Signature format, trusted-key management and offline storage of the signed
  data, digest, signature, pinned key and decision binding.
- Numeric timeout, freshness and cache limits, including retention bounds.

These choices require agreement before implementation. This proposal changes
no runtime code, API, enum, dependency, mode or default hook enablement.

## LineageGate coupling

The pre-merge coupling is a separate consumer of the same evidence. The
janitor authenticates the native audit chain first, then
`verified_tool_call_ids` projects only request ids that have all of the
following:

- one spawn-frozen tool-signing identity;
- an identity envelope valid for that exact run, request, intent, call index,
  audit predecessor, and anchor;
- one immediately following `toolcall.enforced_dispatch` with the same
  attestation reference and intent digest; and
- no duplicate request id or attestation consumption.

The projection does not yet compare the envelope's signed journal head with
the run anchor. The writer refuses to sign after the journal head moves, and
the verdict walk checks the head, but `verified_tool_call_ids` does not; that
check is tracked in #7013.

`LineageEntry.tool_call_id` is matched against that request-id projection.
When authenticated evidence is explicitly supplied, a missing match is a
blocking LineageGate failure surfaced in the janitor signal list. The lineage
gate never opens the audit store and never infers verification mode from a
payload field; it accepts only the frozen set its security caller derived.

Supplying no audit-chain context preserves the existing lineage-only behavior.
That compatibility path is not an attestation claim. Legacy HMAC-only
attestations remain inspectable but are ineligible to authorize a signed
artefact write through this coupling.

## Performance measurement

`scripts/bench_toolcall_interlock.py` compares full
`MCPGateway.handle_jsonrpc` dispatches with and without the enforced seam under
parallel load. The bundled in-process provider isolates host overhead; it does
not measure future signature verification or durable-chain storage. When a
native provider lands, use the same gateway-level harness so the measured path
still includes the actual interlock location.

`scripts/bench_toolcall_identity.py` separately compares the native signed
provider with the native HMAC-only provider at authenticated history depths 1,
1,000, and 10,000 under concurrency 32. It reports signed p95 overhead,
throughput regression, and p95-minus-p50 as a lock-wait proxy against the issue
budgets (1 ms p95 and 10 percent throughput regression).

The warm identity path preserves those invariants without repeating avoidable
work: it parses the frozen Ed25519 keys and immutable RFC 7797 header once,
reuses the already canonical record when hashing the signed envelope, and
skips rescanning self-written history only while a locked re-read proves the
audit head is exactly the provider's last known head. A different head resumes
the authenticated cursor and reconciles other-process attestations before the
next index is allocated. Offline verification still reconstructs the envelope
from first principles; no cache is a trust source.
