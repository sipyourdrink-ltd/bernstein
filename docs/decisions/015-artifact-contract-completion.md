# ADR-015: An Artifact Contract Is a Completion Basis

**Status**: Accepted
**Date**: 2026-09-20
**Context**: `src/bernstein/core/tasks/artifacts.py`,
`src/bernstein/core/tasks/artifact_completion.py`,
`src/bernstein/core/lineage/entry.py`, `docs/operations/artifacts.md`, issue #2608

---

## Problem

The task contract assumed every agent produces code. A coding task completes
when workspace HEAD moves - the git SHA is the completion identity, and the
whole verification chain hangs off it. A non-coding task has no commit to
point at, so a research report, a dataset, an action log or an ops result
tripped the "success without a commit" branch and was treated as a failed
coding run. The output also had no home in the closed lineage kind set, so two
operators running the same non-coding task could not prove they produced the
same artifact.

## Decision

An **artifact contract** is a first-class completion basis, not a log line
alongside a commit.

- A task declares an `ArtifactSpec`: the expected kind, its canonical
  serialisation for byte-identical hashing, and the criteria that close it.
- The completion identity of an artifact-mode task is the **signed lineage
  entry hash** of the artifact's canonical bytes. That is the result's only
  identity. Strip the canonicalisation and two operators disagree on the
  bytes; strip the signature and nobody can attribute them; strip the HMAC
  chain and they can be swapped after the fact.
- Verification stops meaning "run tests" and starts meaning "the artifact
  satisfies its declared contract": schema validity, criteria matching and a
  re-derived hash that matches the recorded one.
- Coding tasks are unchanged. `code_diff` stays the default, and the git
  path is untouched.

## Rejected alternative: record artifacts but keep the commit as the identity

The obvious half-measure is to let a non-coding task reach done with a
placeholder commit or no identity at all. Rejected because an identity that
does not cover the artifact's bytes proves nothing about what was produced.
The receipt has to bind the bytes, or completion is a trust exercise.

## Rejected alternative: widen verification to any predicate

Typed criteria could grow without bound. Rejected in favour of a closed set:
three new signal types, each with a closed evaluator, alongside the original
six. The contract stays checkable by inspection rather than by running
arbitrary code.

## Consequences

### Benefits

Non-coding agents get the same determinism and audit guarantees as a coding
diff, and two operators with equal inputs can prove byte-identical output by
comparing signed lineage-entry hashes.

### Costs

Each new artifact kind is a declared canonicaliser plus criteria, not a patch
to the completion path. That is deliberate: the completion path must not
grow a branch per kind. The canonicalisation itself is the part that must not
drift, and it is pinned by cross-run byte-identity tests.
