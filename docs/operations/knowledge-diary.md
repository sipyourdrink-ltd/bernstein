# Knowledge layer: diary + synthesis

Bernstein keeps a two-tier knowledge layer over task transcripts.

1. **Diary**: a per-task structured entry distilled from the closing
   transcript. Stored at `.sdd/runtime/diaries/<task_id>.json`.
2. **Synthesis**: a periodic aggregation pass that groups diaries into
   themes and drafts a human-readable report at
   `.sdd/runtime/syntheses/<date>.md`.

The synthesis is **HITL-gated**. A normal run writes a report to disk
with `approved: false` in its frontmatter. Use `--apply` to mark the
report approved. A preview (`--dry-run`) prints the report to stdout and
does not write a file; if combined with `--apply`, only the printed report
is marked approved. Without `--dry-run`, `--apply` writes an approved
report to the same dated location as a normal report. No role prompt is
mutated by the synthesizer alone; it always produces review artefacts.

## CLI surface

```bash
# List every diary entry in the active SDD tree.
bernstein knowledge diary list

# Show one entry.
bernstein knowledge diary show task-42

# Run the synthesis pass over the last 14 days.
bernstein knowledge synthesize --since 14d

# Preview to stdout without writing.
bernstein knowledge synthesize --since 7d --dry-run

# Approve and persist (the HITL gate).
bernstein knowledge synthesize --since 7d --apply
```

`--since` accepts `NNd`, `NNh`, `NNm`, `NNs`, or a bare integer (days).
The lookback is converted to whole days, with a one-day minimum; it is not
a rolling hour, minute, or second window. For example, `12h` and `36h`
each select one day, while `49h` selects two days. An integer without a
suffix is interpreted as days.

## Diary shape

Each diary entry carries:

| Field            | Description                                                   |
|------------------|---------------------------------------------------------------|
| `task_id`        | Source task identifier.                                        |
| `tried`          | Bullet list of attempted approaches.                           |
| `worked`         | Bullet list of approaches that succeeded.                      |
| `failed`         | Bullet list of approaches that failed.                         |
| `rationale`      | Free-text explanation.                                         |
| `tags`           | Lower-cased, deduplicated tokens used for clustering.          |
| `redaction_hash` | SHA-256 of the redacted transcript.                            |
| `created_at`     | ISO-8601 timestamp in UTC.                                     |
| `schema_version` | Integer schema version (currently 1).                          |

The diary writer redacts known credential shapes (OpenAI keys, GitHub
tokens, AWS access keys, PEM banners, generic hex bearer tokens)
before hashing or persisting. Verification works on the redacted form,
so two transcripts that differ only in the masked substrings still
verify against the same entry.

## Synthesis algorithm

Clustering uses tag-overlap Jaccard similarity with a configurable
threshold (default `0.34`). Embeddings are deliberately out of scope
for v1; the stdlib implementation is cheap and fully deterministic.
Clusters smaller than `--min-cluster-size` are dropped. Themes are
sorted by size descending so the operator sees the strongest signals
first.

Each theme carries a `proposed_diff` body that summarises recurring
failure patterns and recurring success patterns from the cluster. The
body is markdown shaped like a unified diff (`+`/`-` prefixes); it is a
review aid, not an actual diff that gets applied.

## Storage paths

| Path                                | Purpose                                  |
|-------------------------------------|------------------------------------------|
| `.sdd/runtime/diaries/<task_id>.json` | Per-task diary entry (atomic write).   |
| `.sdd/runtime/syntheses/<date>.md`  | Synthesis report (overwritten per day). |

Both writes go through the atomic-write helper, so a crash mid-flush
never leaves a torn file behind.
