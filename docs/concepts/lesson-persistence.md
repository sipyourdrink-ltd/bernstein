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

`bernstein.core.knowledge.conventions` gives a review correction a durable,
checkable form: a signed, commit-pinned, chain-anchored **convention receipt**
rather than a line of free text that cannot expire, cannot be checked for
conflicts, and cannot be shown to have been in force at a given commit.

Calling `file_review_correction()`:

1. Routes the correction through `file_lesson()` with
   `memory_type=MemoryType.FEEDBACK`, so it lands in the same store and decays
   on the same schedule as every other lesson.
2. Deduplicates against the receipts already on file, so the same correction
   repeated across three reviews leaves one receipt at `version: 3` rather than
   three rules saying the same thing.
3. Binds the rule text hash, subject path or glob, optional subject symbol,
   mandatory base commit SHA, and the executable assertion into one record
   signed with the install's Ed25519 identity.
4. Appends the filing to the HMAC audit chain (`convention.receipt`). Retirement
   and expiry append `convention.retired`; neither is a delete.
5. Rejects a correction that contradicts one already in force, naming both
   receipt ids, instead of accepting both and leaving a reviewer to notice.

`get_active_conventions()` returns the rules in force plus a `ruleset_hash`
computed over what each rule demands — subject, rule text hash, pinned commit,
assertion, version, status — and never over receipt ids or filing times, so two
installs whose active rules agree agree on the digest. A rule whose
`subject_symbol` no longer resolves at HEAD moves to `expired` and appends the
chain entry saying so, so an expired rule is distinguishable from one that was
never filed.

`bernstein verify --memory-audit` covers both lesson memory provenance and the
convention receipts: it runs the audit chain's own verifier and then checks each
receipt's signature and its anchoring entry. A receipt whose rule text was
rewritten in the store, or whose anchor was hand-appended to the log, fails and
is named.

## Limitation: nothing files corrections automatically yet

`file_review_correction()` — like `file_lesson()` before it — is implemented,
tested, and safe to call, but no code path in the shipped orchestrator calls it.
Filing is currently an explicit act: a plugin, a hook, or direct use of the
Python API. Until something in the review path calls it, `.sdd/memory/lessons.jsonl`
and `.sdd/conventions/receipts/` stay empty on a fresh install, and the
read/decay/injection path above remains a no-op — an empty
`## Prior Agent Lessons` block.

General task-completion outcomes are a separate, broader gap: nothing reads a
task's outcome and files a lesson on completion either.


## Related, different subsystem

`spawn_prompt.py` also has a second, unrelated lesson-injection path gated
behind `BERNSTEIN_MEMORY_AUTO_INJECT` (off by default): it tails
`.bernstein/memory/lessons.jsonl` through
`core/memory/jsonl_log.py`'s `JSONLMemoryLog` and renders the most recent
entries as a plain `<lessons>...</lessons>` block. That log has no scoring,
decay, tag matching, or integrity chain — see
[Append-only JSONL memory log](jsonl-memory-log.md). The two systems read
different files and do not share entries.
