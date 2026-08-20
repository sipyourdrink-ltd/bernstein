# Agent lesson persistence

A tag-indexed, decaying store of short lessons filed on task completion.
Lessons are matched against a new task's tags and injected into the spawn
prompt of the agent picking up related work, so a mistake caught once does
not have to be rediscovered by the next agent that touches the same area.

Source: `src/bernstein/core/knowledge/lessons.py` (imported elsewhere as
`bernstein.core.lessons`, a back-compat alias to the same module).

## Storage

Lessons live at `.sdd/memory/lessons.jsonl`, one JSON object per line. Each
entry carries:

| Field | Meaning |
|---|---|
| `lesson_id` | UUID |
| `tags` | Lowercased retrieval tags |
| `content` | The lesson text |
| `confidence` | `0.0`-`1.0`, clamped on write |
| `memory_type` | `user`, `feedback`, `project`, or `reference` (see below) |
| `created_timestamp` | Unix time when first filed |
| `filed_by_agent`, `task_id` | Provenance |
| `version` | Incremented on a confidence update |
| `content_hash`, `prev_hash`, `chain_hash` | Integrity chain (see below) |

### Memory types and decay

`file_lesson()` accepts a `memory_type` that sets how fast the lesson's
effective confidence decays with age:

| Type | Decay half-life |
|---|---|
| `feedback` (human corrections) | 7 days |
| `project` (project conventions) | 14 days |
| `user` (workflow observations, default) | 30 days |
| `reference` (external best practices) | 90 days |

Decay is applied only at read time (`get_lessons_for_agent`); the stored
`confidence` value is not rewritten by age. A lesson older than 1 day also
gets a `**Staleness:**` caveat line appended when rendered into a prompt.

### Deduplication

`file_lesson()` checks the existing JSONL content for a near-duplicate
before appending: same `memory_type`, tag-set Jaccard similarity &ge; 0.75,
and word-overlap content similarity &gt; 0.8. A match updates the existing
entry's `confidence` and bumps `version` instead of creating a new row.

### Integrity chain

Every entry is written with `content_hash` (SHA-256 of the immutable
fields), `prev_hash` (the previous entry's `chain_hash`), and `chain_hash`
(SHA-256 of `content_hash` + `prev_hash`) — see
`src/bernstein/core/knowledge/memory_integrity.py`. Reordering, deleting, or
tampering with an entry breaks the chain from that point forward. New
lesson content also passes through `detect_memory_poisoning()` before it is
accepted; content matching known prompt-injection patterns is rejected with
a `ValueError` and never reaches the file.

Concurrent writers are serialized through a PID/mtime lock
(`memory_lock_protocol.py`) with stale-lock recovery and atomic
backup-then-write, so two agents filing lessons at the same time cannot
corrupt the JSONL file.

### Verifying the chain

```bash
bernstein verify --memory-audit
```

Runs `verify_chain()` over `.sdd/memory/lessons.jsonl` and reports either a
clean chain (entry count, "Tampering: none detected") or the line number of
the first break. Exits `0` on a clean chain, `1` on a violation, `0` if the
file does not exist yet.

## Retrieval and injection

At spawn time, `spawner_core.py` extracts tags from the batch of tasks being
assigned to an agent and calls `gather_lessons_for_context()`. That function:

1. Loads lessons via `get_lessons_for_agent()`, keeping only entries whose
   tags overlap the task tags.
2. Ranks by `tag_overlap + decayed_confidence`, descending.
3. Formats up to 5 lessons as a `## Prior Agent Lessons` markdown block,
   each with type, tags, confidence, source task, and (if stale) a
   staleness note.
4. Truncates at a 5,000-token budget (`DEFAULT_CATEGORY_BUDGETS["lessons"]`
   in `core/tokens/context_compression.py`), appending a truncation notice
   if lessons had to be dropped.

The block is included in every agent's spawn prompt except the `manager`
and `visionary` roles (`SECTION_RULES["lessons"]` in
`core/agents/spawn_prompt.py`). Results are cached per `(role, tags)` pair
for a short TTL so repeated spawns in the same batch do not re-read the
file.

## Convention receipts from review corrections (#3750)

Review corrections persist as signed, commit-pinned, chain-anchored convention
receipts (:mod:`bernstein.core.knowledge.conventions`). When a correction is
filed in the review path via `file_review_correction()`:

1. It routes through `file_lesson()`, classifying the correction with
   `memory_type=MemoryType.FEEDBACK` so it decays appropriately.
2. Deduplication through the lesson store ensures repeating the same correction
   across multiple reviews increments the `version` counter (version 1 → 2 → 3)
   rather than creating duplicate rules.
3. The convention receipt binds the rule text hash, target subject path/glob,
   optional subject symbol, mandatory base commit SHA, and executable assertion
   spec, signed with the install's Ed25519 identity key.
4. Each receipt creation, update, and retirement is recorded as a tamper-evident
   event in the HMAC audit chain (`convention.receipt` / `convention.retired`).
5. `bernstein verify --memory-audit` audits both lesson memory provenance and the
   convention receipts audit chain.
6. Rules pinning a `subject_symbol` automatically expire if the symbol no longer
   resolves at HEAD (verified via AST analysis for Python sources).

## General task-completion limitation

While review corrections now automatically file lessons and convention receipts
(#3750), general task-completion outcomes in the orchestrator do not yet file
general lessons automatically. In practice, workflow observation lessons
(`MemoryType.USER`) require explicit filing or custom hooks.


## Related, different subsystem

`spawn_prompt.py` also has a second, unrelated lesson-injection path gated
behind `BERNSTEIN_MEMORY_AUTO_INJECT` (off by default): it tails
`.bernstein/memory/lessons.jsonl` through
`core/memory/jsonl_log.py`'s `JSONLMemoryLog` and renders the most recent
entries as a plain `<lessons>...</lessons>` block. That log has no scoring,
decay, tag matching, or integrity chain — see
[Append-only JSONL memory log](jsonl-memory-log.md). The two systems read
different files and do not share entries.
