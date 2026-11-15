# Composition-only Agent Manifest export with operator declarations

The optional `bernstein[manifest]` extra pins `agent-manifest[cli]==0.12.0`.
The source reference is upstream `python-v0.12.0`, commit
`9478b56cc349bef01441db4e17e61849c8d69d6f`. `uv.lock` records the package
hashes. This exporter emits version `0.1` JSON using that SDK's signing
preimage and schema. It does not emit the version `0.2` COSE envelope.

The export describes Bernstein's **composed prompt**, before an adapter adds
its own system messages, CLI configuration, attachments or runtime context.
It grants no permissions. The scheduler never reads an exported manifest as
an admission decision. Configured model names are not treated as observed
runtime model identities. All signing happens through the installation's
Ed25519 custody signer; private keys are not exported.

## Artifact slot mapping

| Upstream slot | Available source and export boundary | Export behavior |
| --- | --- | --- |
| `system_prompt` | Composed UTF-8 prompt at the spawner boundary, after prompt precheck, before backend-specific additions | SHA-256 bound; operator supplies version, classification and binding time. This is a composition snapshot, not the adapter's complete system prompt. |
| `policy_bundle` | Resolved gate hashes exist, but are not a complete upstream policy bundle with language/version/enforcement declarations | Unbound; explicit operator reason required. |
| `tool_manifest` | Some adapters have tool schemas; there is no universal complete pre-spawn catalog with dynamic-registration/rug-pull declarations | Unbound; explicit operator reason required. |
| `model_identity` | Configured model exists; actual provider/version/deployment identity is not universally observed at composition time | Unbound; explicit operator reason required. |
| `rag_corpus` | No universal complete corpus snapshot, provenance or ingestion declaration | Unbound; explicit operator reason required. |
| `memory_baseline` | No universal snapshot with memory type, owner and TTL | Unbound; explicit operator reason required; missing memory is not asserted to be absent. |
| `decision_trace` | A run journal is not a declared deploy-time decision-trace baseline | Unbound; explicit operator reason required. |
| `supply_chain` | AI-BOM inventory does not establish an image digest, builder or SLSA assertion for every native agent | Unbound; explicit operator reason required. |
| `delegation_chain` | Existing single-hop links do not establish the upstream multi-link chain contract | Unbound; explicit operator reason required. |
| `hitl_record` | Approval records do not universally supply all upstream human identity, method, time and duration fields | Unbound; explicit operator reason required. |

This deliberately narrow binding does not claim the other artifacts are
absent. Each reason is an operator assertion about what is unavailable for
this export. New bindings require their own observed material and declared
metadata; a digest alone does not fill an upstream artifact object.

## Declarations and explicit export

Install the extra with `uv sync --locked --extra manifest`. Supply a UTF-8
prompt file and a JSON declaration file. The following values are examples;
operators must declare the identity, times, version and reasons for the
actual hop. No current clock, expiry, classification, manifest ID or version
is inferred. Times must have an explicit timezone; expiry must be between one
hour and 365 days after issuance, as upstream requires. The signing time must
fall in the issuance window, and binding must not be later than signing.

```json
{
  "manifest_id": "019c6e27-e55b-73d1-87d8-4e01f1f75043",
  "agent_id": "spiffe://example.test/worker/backend",
  "issuer": "spiffe://example.test/installation",
  "version": "0.1",
  "issued_at": "2026-11-15T08:00:00Z",
  "expires_at": "2026-11-15T10:00:00Z",
  "signed_at": "2026-11-15T08:00:00Z",
  "prompt_bound_at": "2026-11-15T08:00:00Z",
  "prompt_version": "operator-revision-7",
  "classification": "internal",
  "key_type": "software",
  "availability_reasons": {
    "policy_bundle": "Complete policy language/version/enforcement metadata not supplied",
    "tool_manifest": "Complete tool catalog not captured at this boundary",
    "model_identity": "Runtime provider/version not observed before spawn",
    "rag_corpus": "Corpus snapshot and provenance not available",
    "memory_baseline": "Memory snapshot and ownership declaration not available",
    "decision_trace": "Deploy-time baseline and retention declaration not available",
    "supply_chain": "Image and builder provenance not available for this native worker",
    "delegation_chain": "Upstream multi-link delegation contract not available",
    "hitl_record": "Complete upstream human approval metadata not available"
  }
}
```

```sh
bernstein manifest export-agent --declarations declarations.json \
  --prompt composed-prompt.txt --hop-id worker-1 \
  --lineage-root .sdd/lineage/run-1 --key-dir .bernstein/keys
```

This uses `AgentCardKeystore.signer()` for the installation key. Its existing
first-use key generation behavior applies. Outputs go to a new
`agent-manifests/worker-1/` directory beneath the supplied lineage root:
`manifest.json` and `availability.json`. Existing hop directories are refused,
not overwritten. The operator owns and protects the lineage root. Resolver
paths in returned references are relative to that root; retain that base with
the run. Keep the original composed-prompt bytes separately if an independent
consumer will appraise their hash. The exporter never stores prompt text.

Exit 0 and `status: exported` mean the files were written. The CLI explicitly
reports `runtime_artifacts_verified: false`. Invalid declarations, a missing
or different SDK version, a custody failure, or an output collision fail with
exit 1.

## Opt-in spawn integration

Embed the same exporter as `AgentSpawner(..., agent_manifest_exporter=exporter)`:

```python
from bernstein.core.security.agent_card_keystore import AgentCardKeystore
from bernstein.core.security.agent_manifest_export import (
    CompositionDeclarations, CompositionManifestExporter,
)

# declarations_for_hop(hop_id) returns a validated CompositionDeclarations.
# The operator supplies a distinct manifest_id and actual declarations per hop.
exporter = CompositionManifestExporter(
    lineage_root, declarations_for_hop, AgentCardKeystore(key_dir).signer()
)
```

The callback is disabled by default, imports no SDK on ordinary spawn, and
runs before backend launch for fresh and resumed workers. A configured export
failure aborts that spawn. Resumes use new per-hop declarations; no identity
or metadata is borrowed from a prior hop. Already written evidence can remain
if later backend launch fails; it describes the composition attempt, not a
claim that a process successfully ran.

The session carries only the two artifact references. The orchestrator
records them through its existing `artifact_produced` events, scoped by
`agent_id`. The existing per-hop Trust Record exporter cites both digests as
`produced-artifact` references. A sibling's references do not enter the hop.
A callback configured on a standalone spawner writes evidence but does not
create a run journal by itself.

## Availability companion and independent verification

Upstream composition-only has no per-slot reason field and forbids unknown
fields. `availability.json` therefore carries the exact nine-slot reason map,
the SHA-256 of the complete canonical signed `manifest.json`, the manifest
ID, the composition boundary and upstream version reference. A detached
Ed25519 JWS signs the whole companion body with a dedicated
`bernstein-agent-manifest-availability+jws` type. It uses the same custody key.
Changing a reason or substituting another manifest invalidates the binding.
Both files are independently cited by their byte digests in the run record.

`verify_availability_companion(manifest_bytes, companion_bytes,
trusted_public_key_pem)` checks that companion signature, exact unbound set
and manifest digest. The public key must be independently pinned. This helper
does **not** verify the manifest signature or running artifacts; use the
upstream verifier separately, with trusted key material and runtime inputs.

The pinned upstream CLI has no runtime artifact-hash argument:

```sh
manifest verify manifest.json --public-key installation-public.hex
```

It reports `INCOMPLETE` and exits 1 for composition-only, even with an authentic
signature. `--signature-only` also leaves composition-only `INCOMPLETE`; it
cannot demonstrate that any running artifact matched. Do not reinterpret an
exit code or a valid signature as runtime verification.

For actual artifact appraisal, use upstream `verify_manifest` with a
`VerificationContext` carrying independently trusted keys and the observed
`system_prompt_hash`, leaving `strict_artifact_verification=True`. With the
correct prompt hash, `fields_verified.system_prompt` is `MATCH` and the
overall result remains `INCOMPLETE`. With a changed prompt byte it is
`MISMATCH`; with no hash, the binding is unchecked and a warning states that.
No branch converts `INCOMPLETE` to `VALID` or lowers strictness. The dedicated
`agent-manifest-conformance` CI job exercises these cases against the locked
0.12.0 package and invokes its CLI in subprocesses.
