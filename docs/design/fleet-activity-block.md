# Fleet Activity Block — Design

## Context

Issue #4827. The README needs a machine-generated block showing fleet-activity statistics, refreshed by a workflow on a schedule. The block must be reproducible, marker-delimited, and idempotent.

---

## 1 Marker Delimiters

```html
<!-- FLEET-ACTIVITY START -->
```html
<!-- FLEET-ACTIVITY END -->
```

Placed in `README.md` at the insertion point. The delimiters are HTML comments so they survive markdown rendering without appearing in the rendered page.

---

## 2 Block Format

Two serialisation forms are supported — JSON (default) and Markdown (opt-in via a `format: markdown` key). JSON is the canonical form; the workflow emits JSON and a separate rendering step converts it to the Markdown table.

### JSON form

```json
{
  "merged_prs_last_7d": 14,
  "shares_opened_by_fleet": 3,
  "receipts_verified": 47,
  "last_updated": "2026-08-30T12:00:00Z",
  "format": "json"
}
```

### Markdown form

```markdown
| metric | value |
|---|---|
| Merged PRs (last 7d) | 14 |
| Shares opened by fleet | 3 |
| Receipts verified | 47 |
| Last updated | 2026-08-30T12:00:00Z |
```

---

## 3 Stat Derivation

### `merged_prs_last_7d`

**Source:** `git log --merges --since="7 days ago" --first-parent main`

Count the merge commits on `main` within the window. `--first-parent` avoids double-counting merges created by squash-merge or rebase-merge workflows. Each merge corresponds to one PR landing.

**Command:**
```bash
git log --merges --since="${SINCE_EPOCH}" --until="${UNTIL_EPOCH}" --first-parent main --count
```

**Reproducibility:** Uses Unix epoch boundaries (midnight UTC) so the window is deterministic regardless of when the workflow runs during the day.

### `shares_opened_by_fleet`

**Source:** Audit chain event `EVENT_TASK_CLAIM_RECEIPT` (audit chain store, issue #2357)

Each claim receipt represents one task handed to a fleet worker. Count receipt events emitted within the window.

The audit chain is HMAC-chained and replay-detectable, so the count is verifiable against the chain. When the audit chain is absent (e.g. the repo has no `.sdd/`), fall back to zero with a `shares_data_source: audit_chain` / `shares_data_source: unavailable` annotation.

**Reproducibility:** Audit chain entries are immutable and content-addressed; re-scanning the same chain produces the same count.

### `receipts_verified`

**Source:** Run receipts on disk under `.sdd/runs/*/run-receipt.json` or the audit chain event `EVENT_WORK_LEDGER_ANCHOR` (#2358) combined with offline verification calls.

Count receipts that were **verified** (not merely written) within the window. A receipt is "verified" when `bernstein verify receipt` or `bernstein replay verify` succeeded for it. The verification result is stored in the run receipt metadata.

**Reproducibility:** The verification status is part of the receipt envelope; re-running verification on the same receipt produces the same result.

---

## 4 Workflow

### Trigger

- `schedule: "0 */6 * * *"` — every 6 hours, or
- `workflow_dispatch` — manual trigger with `dry_run` boolean input.

### Steps

1. **Compute window.** `SINCE_EPOCH` = now − 7 days (Unix epoch). `UNTIL_EPOCH` = now (Unix epoch).
2. **Collect stats.** Run the three derivation strategies above.
3. **Render block.** Produce the JSON block (canonical form).
4. **Idempotency check.** Read the existing block from `README.md` (if any). Compare with the newly computed block. If identical, exit early without writing.
5. **Write block.** Replace the content between `FLEET-ACTIVITY START` and `FLEET-ACTIVITY END` with the new block.
6. **Commit + PR (optional).** When running on schedule, open a PR with the updated block; when `dry_run: true`, skip write and print to `stdout`.

### Idempotency implementation

```python
# Pseudocode
existing = extract_block(read("README.md"))
new = render_block(stats)

if existing == new:
    print("Block unchanged, skipping update")
    return

write_block("README.md", existing, new)
```

The comparison is byte-exact JSON equality. This ensures the workflow never writes an identical block, avoiding a spurious commit on every run.

### Dry-run

When `dry_run: true`:
- Compute all stats.
- Print the would-be block to `stdout`.
- Do **not** modify `README.md`.
- Exit 0.

---

## 5 Block Extraction — Regex

```python
import re

_BLOCK_PATTERN = re.compile(
    r"<!-- FLEET-ACTIVITY START -->\s*(.*?)\s*<!-- FLEET-ACTIVITY END -->",
    re.DOTALL,
)
```

The block content (captured group 1) is parsed as JSON. Malformed JSON produces a hard failure so silent corruption is impossible.

---

## 6 File Locations

| Artifact | Path |
|---|---|
| Block definition | `docs/design/fleet-activity-block.md` (this file) |
| Workflow | `.github/workflows/fleet-activity.yml` |
| Generation script | `scripts/generate_fleet_activity.py` |
| README insertion point | `README.md` (between HTML comment markers) |

The script is a standalone module importable without bernstein installed (air-gap safe), matching the pattern in `scripts/scrape_ci_postmortems.py`.

---

## 7 Open Questions

1. **`shares_opened_by_fleet` definition** — the task server may not be reachable from the workflow runner in air-gap scenarios. Fallback to zero is safe but loses signal. If the task server API is network-accessible from CI, count `POST /tasks/*/claim` events in the server's own audit log.

2. **`receipts_verified` definition** — offline verification records are on disk; CI does not have access to the `.sdd/` directory of a running system. An alternative: count `EVENT_WORK_LEDGER_ANCHOR` events in the audit chain, which is readable from the git history of a repo that writes the chain as a git note or a tracked file.

3. **Markdown rendering** — decide whether the workflow emits Markdown directly (simpler) or emits JSON and a separate documentation-rendering step produces the Markdown table. JSON-first is more testable; Markdown-first is more human-readable in the diff.
