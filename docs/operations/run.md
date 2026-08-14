# `bernstein run` operator notes

This document covers the `bernstein run` surface and operator-facing
flags. Other run-related docs:

* [`run_names.md`](run_names.md) -- the memorable deterministic run-name
  generator.
* [`runbooks.md`](runbooks.md) -- recovery playbooks for stuck or
  failed runs.

## Image attachments (`--attach`)

`bernstein run` accepts one or more `--attach <path>` arguments to
hand operator-supplied images (screenshots, diagrams) to the spawned
agent. Repeat the flag for multiple files:

```
bernstein run --goal "Reproduce the failure shown" \
  --attach ./screenshot.png \
  --attach ./architecture.svg \
  --cli claude
```

### Capable adapters

Only `claude` and `gemini` accept attachments. Selecting any other
adapter (`codex`, `aider`, `qwen`, ...) with `--attach` aborts the
run BEFORE any process is launched with a `UsageError` that names
the capable adapters.

Verify which adapters are installed and what each one advertises
before pinning `--cli`:

```
bernstein adapters list             # every adapter Bernstein can detect
bernstein adapters check claude     # confirm a specific adapter resolves on PATH
bernstein doctor                    # broader environment smoke test
```

The capability gate uses
`bernstein.core.agents.multimodal.is_multimodal_capable`; the
inventory it consults is authoritative.

### Wire format

Attached files are read at spawn time and inlined into the prompt
body as base64-encoded `<attachment>` blocks at the head of the
prompt:

```
<attachment mime="image/png" sha256="<64 hex chars>">
<base64 payload>
</attachment>

<original prompt body>
```

Both adapters use the same wire format so a replay path can
verify exact bytes regardless of provider.

### Provenance

At spawn time, for every attachment declared for a worker, the
orchestrator:

1. Hashes the raw bytes (SHA-256) and stores them once in the
   content-addressed blob store at `.sdd/cas/`. The hash is taken over
   the bytes that actually travel to the model API, so a source file
   edited between encode and attest cannot desynchronise the record.
2. Appends a `multimodal.attach` event to the HMAC-chained audit log
   carrying `(sha256, mime, operator_install_id_sig, worker_id,
   turn_seq, worktree_id, prev_chain_digest)`. Tampering with the
   on-disk log fails verification.
3. Stamps the resolved digests on the session and its tasks. When such
   a task completes through the artifact path, its signed lineage entry
   carries them in `attachment_digests`, so the receipt names every
   input the turn consumed and not only the code it read.

CAS and the audit chain live under the run's `.sdd/`, not under a
per-session worktree: one shared chain is what lets a cross-worktree
read be refused rather than merely missed.

Replay over the exported chain reproduces the exact bytes the model
API saw on the original turn. Substituting bytes breaks the chain.

`attachment_digests` is an additive, optional field: an entry recorded
for a task with no attachments omits it entirely and keeps the exact
entry hash, HMAC and signature it had before attachments were wired.
The digests are deliberately *not* recorded in `parent_hashes` --
that list is the artefact's own ancestry, and the tip projection reads
two or more parents as a fork merge, so an attached image recorded
there would both mis-type the relation and make every attached linear
write look like a merge.

> The `multimodal-attachment://<sha256>` URI built by
> `lineage_signer.build_attachment_parent_uri` targets the `parents`
> list of the WAL-backed `persistence.lineage.LineageWriter` record,
> which is deprecated and carries no such field. It is unused by the
> shipping path; `attachment_digests` on the signed entry is what a
> verifier reads.

### Isolation modes

Attachments are carried by the direct-subprocess spawn path, where the
capable adapter inlines the encoded bytes from inside its `spawn()`
call. The container, sandbox, runtime-bridge and in-process paths
render their own command or speak a protocol with no attachment slot,
so a run that combines them with attachments is refused up front rather
than silently dropping the bytes between the audit event and the model.

### Worktree pinning

The audit-chain event embeds the worktree id of the attaching
worker. A worker in a different worktree cannot resolve the
attachment back to bytes; the resolver raises
`WorktreeAccessDenied` on cross-worktree attempts. This protects
session-shared state where multiple worktrees coexist.

The resolver reads the `multimodal.attach` events through the
chain's authenticated scan, so the worktree it grants access to is
one the HMAC linkage actually attests. If the chain does not verify
-- for example because something appended an attach row into the
audit directory without the audit key -- the lookup refuses with
`AttachmentChainUnverified` rather than trusting the row. Run
`bernstein audit verify` to see the per-entry errors behind the
refusal.

Crash resume is what reads through that resolver in normal operation.
A resumed worker rebuilds its attachments from CAS rather than
re-reading the operator's files, which may have been edited or deleted
by the crashed agent: the attested bytes are the ones the original turn
sent, so the resumed turn is byte-identical. The read is pinned like
any other, so a resume that lands in a different worktree, or against a
chain that no longer verifies, fails rather than substituting bytes.

### Task YAML

Plan-file steps accept an `attachments:` list:

```yaml
name: Reproduce failure
stages:
  - name: investigate
    steps:
      - title: Describe the screenshot
        role: backend
        attachments:
          - ./screenshot.png
          - ./architecture.svg
```

Plan-declared attachments go through the same dispatch as `--attach`:
CAS storage, the `multimodal.attach` event, the worktree pin, and the
recorded digests all apply. The difference is scope -- a `--attach`
entry applies to every worker the run spawns, a step's `attachments:`
list only to the workers spawned for that task. A path declared both
ways is stored and attested once.

Unlike `--attach`, which Click validates as an existing file, a
plan-declared path is only checked when the worker is about to spawn.
A path that does not resolve to a readable file aborts that spawn with
the offending paths named, rather than running the agent text-only.

`attachments` must be a list: a scalar value (e.g.
`attachments: ./screenshot.png`) fails the plan load with
`PlanLoadError`, and an explicit `null` loads as no attachments.
Non-string list items are currently coerced to strings at load
rather than rejected (#3552 tracks the full
accepted/rejected/defaulted table for plan fields).

## References

* `src/bernstein/core/agents/multimodal_attestation.py` -- the
  attachment primitives: CAS + chain write, capability gate, and the
  authenticated worktree-pinned resolver.
* `src/bernstein/core/agents/attachment_dispatch.py` -- the spawn-path
  seam that calls them, collects the declared paths, stamps the
  digests, and rebuilds the context on resume.
* `src/bernstein/core/security/audit_chain.py` -- the
  `multimodal.attach` event type and the `AuditChainStore` facade.
* `src/bernstein/core/lineage/entry.py` -- `attachment_digests` on the
  signed lineage entry.
