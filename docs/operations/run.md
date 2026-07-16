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

For each `--attach` invocation the orchestrator:

1. Hashes the raw bytes (SHA-256) and stores them once in the
   content-addressed blob store at `.sdd/cas/`.
2. Appends a `multimodal.attach` event to the HMAC-chained audit log
   carrying `(sha256, mime, operator_install_id_sig, worker_id,
   turn_seq, worktree_id, prev_chain_digest)`. Tampering with the
   on-disk log fails verification.
3. Adds the digest to the worker's lineage v1 receipt as a
   `multimodal-attachment://<sha256>` parent so any artefact produced
   this turn carries the input image's hash in its lineage.

Replay over the exported chain reproduces the exact bytes the model
API saw on the original turn. Substituting bytes breaks the chain.

### Worktree pinning

The audit-chain event embeds the worktree id of the attaching
worker. A worker in a different worktree cannot resolve the
attachment back to bytes; the resolver raises
`WorktreeAccessDenied` on cross-worktree attempts. This protects
session-shared state where multiple worktrees coexist.

### Task YAML

Plan-file steps accept an `attachments:` list mirroring the CLI
flag:

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

The orchestrator builds the same `MultiModalContext` from the
listed paths and applies the same capability gate.

## References

* `src/bernstein/core/agents/multimodal_attestation.py` -- spawn-time
  resolver, capability gate, and worktree pinning.
* `src/bernstein/core/security/audit_chain.py` -- the
  `multimodal.attach` event type and the `AuditChainStore` facade.
* `src/bernstein/core/persistence/lineage_signer.py` --
  `register_attachment_parents` for lineage receipt augmentation.
