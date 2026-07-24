# Artifact contract (non-coding tasks)

Audience: operators and reviewers who need to prove a non-coding task
produced a specific report, dataset, action log, or ops result - with the
same guarantees a code diff gets.

## Overview

A coding task's output is a diff. A non-coding task's output is a **report**,
a **dataset**, an **action log**, or an **ops result**. The artifact contract
gives every such output one canonical, byte-stable form and records it as a
signed, content-addressed lineage entry.

The recorded entry **is** the artifact: strip lineage, signing, or the
canonical form and there is only an unattested blob no operator can prove the
agent produced.

## Artifact kinds

| Kind | Canonical form |
|------|----------------|
| `code_diff` | Normalised UTF-8 text (the default; uses the git-diff path, not the artifact sink) |
| `report` | Normalised UTF-8 text |
| `dataset` | Canonical JSONL - one JCS-canonical JSON object per line, `\n`-separated |
| `action_log` | Canonical JSONL (as `dataset`) |
| `ops_result` | A single JCS-canonical JSON object |

A task declares its kind through an `ArtifactSpec` on the task. Absent a spec,
a task is `code_diff` and behaves exactly as before.

## Canonicalisation rules (shared core)

Every kind routes through one core so two kinds can never disagree:

- **Stable key ordering** - JSON objects serialise with sorted keys.
- **Fixed UTF-8** - no ASCII escaping, no BOM.
- **Normalised newlines** - CRLF and lone CR fold to `\n`.
- **Reject, don't repair** - text that is not NFC-normalised is *rejected*,
  not silently normalised, so two byte-different inputs can never both pass as
  "the same" artifact. NaN / Infinity are rejected in JSON kinds.

The artifact's identity is `content_hash = sha256(canonical_bytes)`.

## Determinism

The signed lineage entry for an artifact is a deterministic projection of
`(task_id, kind, artifact)`: the tool-call id, span id, and timestamp are
derived from the task, not the wall clock. Two operators who run the same task
with the same inputs produce:

- the **byte-identical `content_hash`**, and
- the **identical signed lineage-entry hash** (the completion receipt).

A one-byte change to the input changes both.

## Verification criteria

A task's `ArtifactSpec` may declare typed criteria evaluated against the
artifact bytes (in addition to the six filesystem/test completion signals):

| Criterion | Checks |
|-----------|--------|
| `hash_stable` | Re-derives the canonical hash and compares it to an expected `sha256:...` |
| `schema_valid` | Validates the artifact's JSON document against a declared JSON Schema (JSONL kinds validate each row) |
| `criteria_match` | Evaluates a closed predicate set (`exists` / `eq` / `ne` / `contains` / `gt` / `ge` / `lt` / `le`) over the JSON document |

Each has a closed evaluator; none executes artifact-supplied code.

## `bernstein artifact verify`

```
bernstein artifact verify <task_id> [--workdir .] [--output-json]
```

The command:

1. Re-derives the canonical hash from the stored artifact bytes and confirms
   it matches the receipt - a post-hoc byte alteration of the blob fails here.
2. Ties the blob to the signed lineage entry named by the receipt - a removed
   entry or a swapped hash fails here.
3. Runs the lineage gate: every entry's Ed25519 signature verifies, the
   operator HMAC chain is intact, and no `parent_hash` dangles.

Exit codes: `0` = verified, `2` = tampered / missing / unverifiable.

The operator HMAC secret is read from `$BERNSTEIN_OPERATOR_SECRET`, falling
back to the audit key. When no secret is available the HMAC leg is skipped;
the Ed25519 signature and parent-chain checks still run.

### On-disk layout

```
.sdd/
  lineage/log.jsonl                # signed, HMAC-chained lineage log
  lineage/signatures/…             # detached JWS sidecars
  agents/<agent-id>/card.json      # Agent Cards (public keys)
  artifacts/<task_id>/artifact.bin # canonical artifact bytes (content sink)
  artifacts/<task_id>/receipt.json # pointer to the signed entry (re-checked on verify)
```

## Source

- `src/bernstein/core/tasks/artifacts.py` - kinds, canonicalisers, criteria.
- `src/bernstein/core/lineage/artifact_record.py` - record + verify.
- `src/bernstein/core/lineage/entry.py` - the widened, still-closed
  `ARTEFACT_KINDS`.
- the `artifact` group in `src/bernstein/cli/commands/artifact_cmd.py`.

## Scope

This is the typed contract layer. Wiring the artifact path into the adapter
`output_mode` axis, skipping worktree allocation for artifact-mode tasks, and
the `commit_completion` branch are a separate follow-up; a coding task stays on
the git-diff path and is unchanged.
