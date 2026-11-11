# C2PA content credentials

Bernstein already writes a signed, Merkle-chained lineage spine for every
artifact a run produces (see [Lineage v1](../lineage.md)).
`bernstein credential emit` and `bernstein credential
verify` turn that spine into a machine-readable
[C2PA](https://c2pa.org/) 2.4 manifest an operator can publish alongside
media or documents, so a downstream verifier can check provenance without
trusting Bernstein's word for it.

The manifest is a **projection** of the spine, not a separately-asserted
label: with no lineage entry for the artifact, there is nothing to project,
so `emit` fails rather than fabricating an unsigned credential.

## Emitting a credential

```
bernstein credential emit ARTIFACT --run-id RUN_ID [--workdir DIR] [--json]
```

Reads the lineage spine for `RUN_ID`, projects `ARTIFACT`'s subtree of spine
entries into a C2PA manifest, and signs it with the install-identity Ed25519
key. By default the manifest is written next to the artifact as
`<artifact>.c2pa.json`; `--json` prints the manifest to stdout instead of
writing a file. Exit codes: `0` written, `1` no lineage entry for the
artifact or bad input (e.g. the artifact path resolves outside the workspace
root).

The manifest carries:

- a **hard binding** assertion (`c2pa.hash.data`) — the spine entry's content
  hash, binding the manifest to the exact bytes the chain recorded;
- an **AI actions** assertion (`c2pa.actions`) — the producing model and
  actor, drawn from the spine entry, plus the IPTC digital source type the
  lineage window supports (see below);
- an **AI disclosure** assertion (`c2pa.ai-disclosure`) — attached when a
  lineage step named a model, naming that model and the step it served;
- an optional **soft binding** assertion (`c2pa.soft-binding`), emitted only
  when a watermark or fingerprint layer is plugged in — the projection does
  not couple to any one watermarking scheme.

## AI disclosure

C2PA 2.4 added the AI-disclosure assertion (§18.28 of the specification), and
the projection emits it from lineage facts rather than from a separately
maintained label. The rule is the same one the AI BOM reads: a lineage step
that recorded a model (`model="anthropic:claude"`) was served by that model; a
step that recorded no model (`model=""`) was not.

- The window **named a model** → `c2pa.ai-disclosure` is attached for the
  most recent model-served step, and the actions assertion's
  `digitalSourceType` is `trainedAlgorithmicMedia` when that step was the
  write that landed last, `compositeWithTrainedAlgorithmicMedia` when a human
  write landed on top of model output.
- The window **named no model** → no disclosure assertion is attached at all,
  and `digitalSourceType` is `digitalCreation`. A credential never claims AI
  involvement the chain does not evidence.

Fields the spine cannot evidence are left out rather than filled in: the
assertion carries the generic `c2pa.types.model` model type (the spine
records a model string, not a model family) and no
`contentProfile.humanOversightLevel` (the spine records who wrote a step,
never whether a human reviewed a model's output; both are optional in the
spec's CDDL schema).

```json
{
  "label": "c2pa.ai-disclosure",
  "data": {
    "modelType": "c2pa.types.model",
    "modelName": "anthropic:claude",
    "metadata": {
      "actor": "agent-a",
      "step_id": "step-1",
      "lineage_entry_hash": "sha256:270b4ac66638195b6c5a0c91575ff2a8995de56e5bbd894b42f2e5b8265d0755"
    }
  }
}
```

`metadata` carries the step and entry hash the disclosure was projected from,
so a verifier can resolve the claim back into the spine — the same link the
actions assertion carries for the bound step.

## Verifying a credential

```
bernstein credential verify ARTIFACT [--workdir DIR] [--manifest PATH]
```

Re-reads the manifest (defaults to `<artifact>.c2pa.json`, override with
`--manifest`), recomputes the hard-binding hash from the artifact's current
bytes, and checks the Ed25519 signature against the install identity. Exit
codes: `0` OK (hard binding matches, signature chains to the install
identity), `1` bad input (no manifest at the expected path, or the manifest
does not parse), `2` verification failed (hash mismatch or bad signature —
the artifact was edited after the credential was issued, or the credential
was forged).

## Guarantees

- **Determinism.** `project_manifest` is a pure function of its inputs — it
  never reads a clock, environment, or socket. The canonical signing bytes
  are sorted-key, compact-separator JSON, and Ed25519 signing is
  deterministic (RFC 8032), so two replays of the same run produce
  byte-identical manifests, including assertion order and signature bytes.
- **One attestation root.** Both "who ran this" (the install identity) and
  "what was produced" (the content credential) are covered by the same
  Ed25519 key, so a verifier checks one signature to trust both claims.
- **Disclosure follows the chain.** The AI-disclosure assertion is emitted
  only when a lineage step named a model, and names that step: a credential
  cannot claim AI involvement the spine does not evidence, and an artifact no
  model touched is projected as `digitalCreation` with no disclosure at all.
- **Unproducible without provenance.** `emit` cannot manufacture a credential
  for an artifact with no lineage-spine entry; the projection has nothing to
  draw from, so it raises rather than emitting an unsigned or fabricated
  label.

## Source

`src/bernstein/cli/commands/credential_cmd.py` (CLI),
`src/bernstein/core/lineage/c2pa.py` (manifest projection, signing,
verification), `src/bernstein/core/lineage/spine.py` (the lineage spine the
manifest projects from).
