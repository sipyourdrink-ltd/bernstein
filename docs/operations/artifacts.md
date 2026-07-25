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

## Completing a task on a receipt instead of a commit

A task whose `ArtifactSpec` declares any kind other than `code_diff` is in
**artifact mode**. It completes without a commit: the orchestrator reads the
artifact the agent produced, evaluates every declared completion signal against
it, and - only when they all pass - records the signed lineage entry whose hash
is the task's completion identity. That entry hash is what a git SHA is for a
coding task.

### Declaring one

```yaml
artifact_spec:
  kind: report
  output_path: reports/weekly.md      # workdir-relative; must not escape the workdir
completion_signals:
  - type: schema_valid
    value: '{"type": "object", "required": ["id"]}'
  - type: hash_stable
    value: 'sha256:...'
```

`output_path` is where the agent writes its deliverable. Leave it empty and the
task defaults to `.sdd/outbox/<task-id>/artifact`.

The bytes are read in the shape the kind expects: JSONL rows for `dataset` and
`action_log`, a JSON object for `ops_result`, text for `report` (or a figures
bundle when the file is one - see [Figure grounding](#figure-grounding-report-artifacts)).

### What the operator gets

| Outcome | Result |
|---|---|
| Every signal passes | Canonical bytes + `receipt.json` under `.sdd/artifacts/<task-id>/`, a signed entry in `.sdd/lineage/log.jsonl`, task marked done |
| Any signal fails | **No receipt.** The task fails with the per-signal detail, exactly as a failing coding task does |
| No artifact written | The task fails with `wrote no output at <path>` |
| `output_path` is absolute or contains `..` | Rejected before any bytes are read |

A receipt asserts that the declared gates held, so a failing gate never mints
one. Verify any receipt afterwards with `bernstein artifact verify` (below).

### Why the commit check does not fire

`decide_retry` in `commit_completion` consults the run's **output mode** before
the HEAD verdict. An artifact-mode run has no commit, so an unmoved HEAD is the
contract rather than a defect and the "you exited without committing" nudge is
never sent. The mode comes from the adapter's declared `output_mode` axis
(every shipped adapter declares `git-diff`) and can be overridden per task, so
one adapter can drive both a coding task and a report task.

### Signing identity

The receipt is signed by a stable identity, `agent:artifact-completion`,
provisioned on first use under `.sdd/artifacts/identity/` and published as an
Agent Card at `.sdd/agents/agent:artifact-completion/card.json` where the
lineage gate reads it. The `agent_id` and `kid` are constants and the private
key signs a detached sidecar, so two installs holding different keys still
produce the identical entry hash for the same artifact.

## Verification criteria

A task's `ArtifactSpec` may declare typed criteria evaluated against the
artifact bytes (in addition to the six filesystem/test completion signals):

| Criterion | Checks |
|-----------|--------|
| `hash_stable` | Re-derives the canonical hash and compares it to an expected `sha256:...` |
| `schema_valid` | Validates the artifact's JSON document against a declared JSON Schema (JSONL kinds validate each row) |
| `criteria_match` | Evaluates a closed predicate set (`exists` / `eq` / `ne` / `contains` / `gt` / `ge` / `lt` / `le`) over the JSON document |
| `figures_grounded` | On a report bundle, requires every declared figure's anchor to resolve to a verifying lineage record and every material number in the body to be declared (see [Figure grounding](#figure-grounding-report-artifacts)) |

Each has a closed evaluator; none executes artifact-supplied code.

These four criteria are only evaluable with the produced artifact in scope. The
janitor's filesystem verification path cannot see it, so it reports them as
**not passed** with the detail
`<type> requires artifact-mode evaluation via evaluate_artifact_signals()`.
That is the deliberate default: a declared completion gate that no evaluator
checked must never read as verified. Declare an artifact-mode criterion only on
a task that is completed through the artifact path; on a task verified from the
filesystem it will hold the task open rather than pass silently.

## `bernstein artifact verify`

```
bernstein artifact verify <task_id> [--workdir .] [--output-json]
```

This command is **task-keyed**: it proves one task's receipt. To ask the same
kind of question about an *output* rather than a task - who produced the current
tip of a package or a release PR, and is it good and current - use the
URI-keyed commands documented in
[Artifact keys](../lineage/artifacts.md#inspecting-an-artifact):
`bernstein artifact list`, `artifact log <uri>`, `artifact health <uri>`.

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

## Figure grounding (report artifacts)

A schema-valid report can still be **fabricated**: every number in the prose
can come from the model, and `schema_valid` / `criteria_match` / `hash_stable`
only prove the artifact's *shape*, never its *claims*. Figure grounding closes
that gap for `report`-kind artifacts: every material number must trace to an
anchored source, or the task does not complete.

### The `figures.json` sidecar

A grounded report is recorded as a **bundle** - the prose body plus a
`figures.json` sidecar - serialised as one canonical JSON object
(`{"body": ..., "figures": [...]}`). The sidecar is therefore *inside* the
artifact's own `content_hash`: editing a figure value after completion changes
the hash (the same hash-stability machinery above), so a figure cannot be
altered without breaking the signed record.

Each figure declares:

| Field | Meaning |
|-------|---------|
| `value` | The number as written (e.g. `"1,234"`, `"$4.5M"`, `"12.5"`) |
| `unit` | Its unit (`"users"`, `"%"`, `"GB"`, ...) |
| `label` | A human-readable name for the figure |
| `anchor` | `{kind, ref}` - the lineage record that grounds it |

Anchor kinds available today: `attachment` and `artifact` (both a `sha256:`
content hash of a signed lineage record). `receipt` (a query-receipt id) is a
reserved plug point - the resolver registry accepts it so the receipt kind lands
without reworking this contract.

### The `figures_grounded` completion signal

A closed evaluator (no network) that runs two checks:

1. **Anchors resolve.** Every declared figure's anchor must resolve to a
   lineage record that verifies - Ed25519 signature (kid-bound), operator HMAC
   (when a secret is available), and chain anchoring (its parents are present).
   A tampered or missing target record fails the figure.
2. **Every material number is declared.** A unit- and locale-aware tokenizer
   scans the body; every *material* number (quantity, currency amount,
   percentage, count) must appear in the sidecar. The failure names each
   unanchored number with its line and column.

The false-positive policy is pinned by an extensible vector suite
(`tests/unit/tasks/data/figure_tokenizer_vectors.json`):

| Exempt (no anchor demanded) | Material (anchor demanded) |
|-----------------------------|----------------------------|
| Section numbers (`§3.2`, `Section 4`, `Figure 2`) | Currency (`$1,234.56`, `€49`, `$4.5M`, `USD 2,000`) |
| ISO dates (`2026-07-24`, `2026-07-24T09:30`) and bare years | Percentages (`12.5%`, `30 percent`) |
| Versions (`v3.9.0`, `1.2.3`) | Quantities (`3.2 GB`, `250 ms`) |
| Allowlisted patterns (policy regexes, e.g. `24/7`) | Counts (`1,234`, `5000`, decimals) and ranges (`10-20%`) |
| Identifier-glued numbers (`P99`, `IPv4`) | |
| Bare integers below the materiality floor (default 1000) | |

Tune the policy with `TokenizerPolicy` (`materiality_min`, `units`, `allowlist`).

### Failure semantics: strict by default, `warn` per task

An unanchored figure is a **completion failure**, not a warning - the same
posture as an unverifiable artifact hash. The signal's `value` sets the
severity:

| `value` | Behaviour |
|---------|-----------|
| `""` / `"strict"` (default) | An ungrounded figure fails completion |
| `"warn"` | Downgrade: the failure is reported with a `WARN:` prefix but does not block completion (exploratory work) |

`bernstein artifact verify` is the audit tool and is always strict: it renders
a per-figure provenance line and exits non-zero on any failing figure,
regardless of the task's severity.

### `artifact verify` output

For a grounded report, `artifact verify <task_id>` adds a figures section:

```
VERIFIED task=RPT-1
  content_hash  sha256:…
  entry_hash    sha256:…
  figures:
    OK migrated users (1,234) - traces to artifact sha256:9f2c1a…, recorded at chain position 3
```

A failing figure renders `UNANCHORED <number> (<category>) at line L, col C`
and the command exits `2`.

## Source

- `src/bernstein/core/tasks/artifacts.py` - kinds, canonicalisers, criteria.
- `src/bernstein/core/tasks/figures.py` - figure tokenizer, `figures.json`
  sidecar, report bundle, and the pure `figures_grounded` evaluator.
- `src/bernstein/core/lineage/figure_grounding.py` - the lineage-wired anchor
  resolver (attachment / artifact today, receipt plug point) and
  `verify_report_figures`.
- `src/bernstein/core/lineage/artifact_record.py` - record + verify (records a
  report bundle; the figures verdict is part of `verify_artifact`).
- `src/bernstein/core/lineage/entry.py` - the widened, still-closed
  `ARTEFACT_KINDS`.
- `src/bernstein/core/tasks/artifact_completion.py` - the completion path:
  load, evaluate every signal with the artifact in scope, record the receipt.
- `src/bernstein/adapters/_contract.py` - the `output_mode` strategy axis.
- the `artifact` group in `src/bernstein/cli/commands/artifact_cmd.py`.

## Scope

The typed contract, figure grounding for report artifacts, the `output_mode`
adapter axis, and the completion path that records a receipt instead of a git
SHA. A coding task stays on the git-diff path and is unchanged: `code_diff` is
still the default kind, every shipped adapter still declares `git-diff`, and the
filesystem completion signals still evaluate exactly as before.

Not yet wired: skipping worktree allocation for artifact-mode tasks (an
artifact task is allocated a worktree it does not need), and the provider-batch
path in `batch_api`, which commits by construction and so stays git-only.
