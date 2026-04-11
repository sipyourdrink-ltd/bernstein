# WORKFLOW: Semantic Deduplication of Similar Tasks Across Plan Stages
**Version**: 0.1
**Date**: 2026-04-11
**Author**: Workflow Architect
**Status**: Draft
**Implements**: road-069 — Semantic deduplication of similar tasks across plan stages

---

## Overview

Detect semantically duplicate tasks at plan load time — tasks like "Add input validation to UserController" and "Validate user input in UserController" that describe the same work with different wording. Uses embedding-based cosine similarity (local model, no external API) to identify duplicates across plan stages and within the LLM planner output. Warns the operator and optionally auto-merges duplicates before tasks are submitted to the task server.

**Distinct from**: `duplicate_detector.py` (word-overlap Jaccard similarity on title+description — exists but unused), `request_dedup.py` (HTTP idempotency keys), `section_dedup.py` (prompt section caching), `semantic_cache.py` (planning goal caching + task result reuse). This is task-level semantic deduplication at the plan ingestion boundary.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Plan Loader (`plan_loader.py`) | Parses YAML plan → list of Tasks; triggers deduplication before returning |
| LLM Planner (`planner.py`) | Generates tasks from natural-language goal; triggers deduplication before submitting to server |
| Semantic Deduplicator (new component) | Computes embeddings, finds duplicate clusters, produces `DeduplicationReport` |
| Embedding Backend | Computes text embeddings — TF-IDF (zero-dep default) or sentence-transformers `gte-small` (if installed) |
| Operator (CLI user) | Reviews deduplication warnings; decides whether to accept auto-merge or override |
| Task Server (`routes/tasks.py`) | Receives deduplicated task list; no duplicate awareness needed at this layer |

---

## Prerequisites

- Plan file (YAML) with `stages` and `steps` — or LLM planner output with generated tasks
- Embedding backend available:
  - **Default (zero-dep)**: TF-IDF word-frequency vectors with cosine similarity (already implemented in `semantic_cache.py` → `_embed()` and `_cosine()`)
  - **Enhanced (optional)**: `sentence-transformers` with `gte-small` model (already used by `embedding_scorer.py` for file relevance)
- `duplicate_detector.py` exists with merge logic but is currently unwired — can be extended or replaced

---

## Trigger

1. **Plan load**: `load_plan(path)` called from `run_cmd.py` or `plan_generate_cmd.py` — deduplication runs after YAML parsing, before task list is returned
2. **LLM planning**: `plan()` called from `planner.py` — deduplication runs after LLM response parsing, before tasks are submitted to task server
3. **Batch task creation**: `POST /tasks/batch` — optional server-side deduplication check (warn-only, never block)

---

## Workflow Tree

### STEP 1: Task Extraction
**Actor**: Plan Loader or LLM Planner
**Action**: Parse input source into a list of candidate `Task` objects with title, description, role, stage_index, and step_index
**Timeout**: N/A (synchronous parsing)
**Input**: YAML plan file or LLM planner JSON output
**Output on SUCCESS**: `list[Task]` with N candidate tasks → GO TO STEP 2

**Output on FAILURE**:
- `FAILURE(parse_error)`: Invalid YAML or malformed LLM output → return error to caller; no tasks created
- `FAILURE(empty_plan)`: Plan has zero steps → return empty list; log warning

**Observable states**:
- Customer sees: N/A (parsing phase)
- Operator sees: N/A
- Database: N/A
- Logs: `[plan_loader] parsed N tasks from plan`

---

### STEP 2: Embedding Computation
**Actor**: Semantic Deduplicator
**Action**: Compute text embeddings for each task's semantic key: `f"{title}\n{description}"`. Use available backend (TF-IDF default, sentence-transformers if installed).
**Timeout**: 10s for TF-IDF (local, fast); 60s for sentence-transformers (model load on first call)
**Input**: `list[Task]` from Step 1
**Output on SUCCESS**: `list[TaskEmbedding(task_index, embedding_vector)]` → GO TO STEP 3

**Embedding key construction**:
```python
def _task_semantic_key(task: Task) -> str:
    return f"{task.title}\n{task.description}"
```

**Backend selection** (automatic, no config needed):
```
if sentence_transformers available and model cached:
    use gte-small (384-dim dense vectors, higher accuracy)
else:
    use TF-IDF word-frequency vectors (sparse, zero-dep, good enough for 80% of cases)
```

**Output on FAILURE**:
- `FAILURE(embedding_timeout)`: Model load or computation exceeded timeout → fall back to TF-IDF; if TF-IDF also fails → skip deduplication entirely, log warning, return original task list unchanged
- `FAILURE(model_load_error)`: sentence-transformers model not found → fall back to TF-IDF (graceful)

**Observable states**:
- Customer sees: N/A
- Operator sees: N/A
- Database: N/A
- Logs: `[dedup] computed embeddings for N tasks using backend=tfidf|gte-small`

---

### STEP 3: Pairwise Similarity Computation
**Actor**: Semantic Deduplicator
**Action**: Compute cosine similarity between all task pairs. Use O(n²/2) pairwise comparison (acceptable for plan sizes — typically 5-50 tasks; plans >200 tasks would need ANN index, see Open Questions).
**Timeout**: 5s (n² for n=50 is 1,225 comparisons — trivial)
**Input**: `list[TaskEmbedding]` from Step 2
**Output on SUCCESS**: `list[SimilarityPair(task_i, task_j, score)]` where score ≥ threshold → GO TO STEP 4

**Thresholds**:
```
HIGH_CONFIDENCE_DUPLICATE = 0.92   # auto-merge candidate (titles are near-identical)
LIKELY_DUPLICATE = 0.80            # warn operator; suggest merge
RELATED_BUT_DISTINCT = 0.65        # informational only; may share scope but are different tasks
```

**Scope restriction**: Only compare tasks with the **same role** or **overlapping `owned_files`**. Tasks with different roles targeting different files are unlikely to be semantic duplicates even if descriptions overlap (e.g., "Add validation" for backend vs frontend are distinct tasks).

**Output on FAILURE**:
- `FAILURE(computation_error)`: Numerical error in cosine similarity → skip deduplication, return original list, log error

**Observable states**:
- Customer sees: N/A
- Operator sees: N/A
- Database: N/A
- Logs: `[dedup] found M candidate duplicate pairs above threshold=0.80`

---

### STEP 4: Duplicate Cluster Formation
**Actor**: Semantic Deduplicator
**Action**: Group similar pairs into clusters using transitive closure. If A~B and B~C, then {A, B, C} is one cluster. Select a "canonical" task per cluster (highest priority, or earliest stage if equal priority).
**Timeout**: N/A (O(n) union-find)
**Input**: `list[SimilarityPair]` from Step 3
**Output on SUCCESS**: `list[DuplicateCluster(canonical_task, duplicate_tasks, max_similarity)]` → GO TO STEP 5

**Cluster structure**:
```python
@dataclass
class DuplicateCluster:
    canonical: Task          # the task to keep
    duplicates: list[Task]   # tasks to merge into canonical
    max_similarity: float    # highest pairwise score in cluster
    strategy: str            # "auto_merge" | "warn" | "info"
```

**Canonical selection rules** (in priority order):
1. Task with higher `priority` value (1=critical wins over 2=normal)
2. Task in earlier stage (stage_index lower wins)
3. Task with longer description (more detail wins)
4. First task by step_index (tie-breaker)

**Strategy assignment**:
- `max_similarity >= 0.92` → `"auto_merge"` (high confidence)
- `0.80 <= max_similarity < 0.92` → `"warn"` (operator decides)
- `0.65 <= max_similarity < 0.80` → `"info"` (logged but no action)

**Observable states**:
- Customer sees: N/A
- Operator sees: N/A
- Database: N/A
- Logs: `[dedup] formed K clusters: X auto-merge, Y warn, Z info`

---

### STEP 5: Deduplication Report Generation
**Actor**: Semantic Deduplicator
**Action**: Produce a `DeduplicationReport` with all clusters, actions taken, and warnings. Print human-readable summary to stderr (CLI output).
**Timeout**: N/A (formatting)
**Input**: `list[DuplicateCluster]` from Step 4
**Output on SUCCESS**: `DeduplicationReport` → GO TO STEP 6

**Report structure**:
```python
@dataclass
class DeduplicationReport:
    total_tasks: int
    clusters: list[DuplicateCluster]
    auto_merged: int          # count of tasks removed by auto-merge
    warnings: int             # count of clusters needing operator review
    deduplicated_tasks: list[Task]  # final task list after auto-merges
```

**CLI output format** (stderr, human-readable):
```
[dedup] Scanned 24 tasks across 4 stages
[dedup] Found 3 duplicate clusters:
  [AUTO-MERGE] "Add input validation to UserController" (stage 2, step 3)
    ≈ "Validate user input in UserController" (stage 3, step 1) [similarity=0.94]
    → Keeping stage 2 version; merging description details
  [WARNING] "Write unit tests for auth module" (stage 1, step 5)
    ≈ "Add test coverage for authentication" (stage 3, step 4) [similarity=0.85]
    → Review recommended: similar but may have different scope
  [INFO] "Refactor database queries" (stage 2, step 2)
    ≈ "Optimize slow database calls" (stage 4, step 1) [similarity=0.71]
    → Likely related but distinct tasks
[dedup] Result: 24 → 23 tasks (1 auto-merged, 1 warning)
```

**Observable states**:
- Customer sees: Deduplication summary in CLI output
- Operator sees: Same CLI output; warnings highlighted
- Database: N/A (report is transient)
- Logs: `[dedup] report: total=24 auto_merged=1 warnings=1`

---

### STEP 6: Task List Deduplication (Auto-Merge)
**Actor**: Semantic Deduplicator
**Action**: For `auto_merge` clusters, remove duplicate tasks from the list and enrich the canonical task with details from duplicates. For `warn` clusters, keep all tasks but add metadata.
**Timeout**: N/A (list manipulation)
**Input**: `DeduplicationReport` from Step 5
**Output on SUCCESS**: Deduplicated `list[Task]` → RETURN to caller (plan_loader or planner)

**Merge rules** (for auto-merge clusters):
1. **Title**: Keep canonical task's title
2. **Description**: Append unique sentences from duplicate's description that aren't in canonical's description
3. **Priority**: Keep highest priority (lowest number) across cluster
4. **Owned files**: Union of all `owned_files` across cluster
5. **Completion signals**: Union of all `completion_signals` across cluster
6. **Complexity**: Keep highest complexity across cluster
7. **Estimated minutes**: Keep maximum estimate across cluster
8. **Dependencies**: Union of all `depends_on` across cluster (with duplicate dependency removal)
9. **Stage/step**: Keep canonical's placement

**Metadata enrichment** (for warn/info clusters):
```python
task.metadata["dedup_cluster_id"] = cluster_id
task.metadata["dedup_similarity"] = max_similarity
task.metadata["dedup_related_tasks"] = [t.title for t in cluster.duplicates]
```

**Output on FAILURE**:
- `FAILURE(merge_error)`: Exception during field merging → skip merge for this cluster, keep all tasks, log error

**Observable states**:
- Customer sees: Final task count in CLI output (e.g., "24 → 23 tasks")
- Operator sees: Merged task has enriched description; metadata tracks provenance
- Database: N/A (tasks not yet submitted to server)
- Logs: `[dedup] auto-merged task "{duplicate_title}" into "{canonical_title}"`

---

### STEP 7: Persistence of Deduplication History
**Actor**: Semantic Deduplicator
**Action**: Append deduplication decisions to `.sdd/caching/dedup_history.jsonl` for audit trail and threshold tuning.
**Timeout**: N/A (file append)
**Input**: `DeduplicationReport` from Step 5
**Output on SUCCESS**: History persisted → END

**Record format**:
```json
{
  "timestamp": "2026-04-11T14:30:00Z",
  "plan_source": "plans/my-project.yaml",
  "total_tasks": 24,
  "clusters": [
    {
      "canonical": "Add input validation to UserController",
      "duplicates": ["Validate user input in UserController"],
      "similarity": 0.94,
      "strategy": "auto_merge",
      "action_taken": "merged"
    }
  ],
  "backend": "tfidf",
  "thresholds": {"auto_merge": 0.92, "warn": 0.80, "info": 0.65}
}
```

**Observable states**:
- Customer sees: N/A
- Operator sees: Audit trail in `.sdd/caching/dedup_history.jsonl`
- Database: N/A (file-based)
- Logs: `[dedup] persisted dedup history for plan=my-project.yaml`

---

## State Transitions

```
[raw_tasks] -> (embedding) -> [embedded_tasks]
[embedded_tasks] -> (similarity) -> [scored_pairs]
[scored_pairs] -> (clustering) -> [clustered]
[clustered] -> (report) -> [reported]
[reported] -> (auto-merge applied) -> [deduplicated_tasks]
[deduplicated_tasks] -> (returned to caller) -> [tasks_ready_for_server]
```

---

## Handoff Contracts

### Plan Loader → Semantic Deduplicator (in-process)
```
PAYLOAD: list[Task] — parsed but not yet submitted
SUCCESS RESPONSE: DeduplicationReport with deduplicated_tasks: list[Task]
FAILURE RESPONSE: Original list[Task] unchanged (graceful degradation)
TIMEOUT: 30s total (embedding + similarity + clustering)
ON FAILURE: Return original task list; log warning; never block plan loading
```

### LLM Planner → Semantic Deduplicator (in-process)
```
PAYLOAD: list[Task] — generated by LLM, parsed from JSON
SUCCESS RESPONSE: DeduplicationReport with deduplicated_tasks: list[Task]
FAILURE RESPONSE: Original list[Task] unchanged
TIMEOUT: 30s total
ON FAILURE: Return original task list; log warning; never block planning
```

### Semantic Deduplicator → Embedding Backend (in-process)
```
PAYLOAD: list[str] — semantic keys (title + description)
SUCCESS RESPONSE: list[vector] — embedding vectors (sparse dict or dense array)
FAILURE RESPONSE: None — triggers fallback to TF-IDF or skip
TIMEOUT: 10s (TF-IDF) / 60s (sentence-transformers model load)
ON FAILURE: Fall back to lower-fidelity backend; if all fail, skip dedup
```

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Embedding vectors (in-memory) | Step 2 | Garbage collection | Function returns; locals freed |
| Similarity pairs (in-memory) | Step 3 | Garbage collection | Function returns; locals freed |
| Duplicate clusters (in-memory) | Step 4 | Garbage collection | Function returns; locals freed |
| Dedup history JSONL entry | Step 7 | N/A (append-only audit trail) | Manual cleanup if disk space needed |

No external resources created. No cleanup needed on failure — all state is transient in-memory until the final JSONL append.

---

## Graceful Degradation Rules

This workflow must **never** block task creation. Deduplication is advisory/optimization, not control flow.

1. If embedding computation fails → return original task list unchanged
2. If similarity computation fails → return original task list unchanged
3. If sentence-transformers not installed → fall back to TF-IDF (zero-dep)
4. If TF-IDF also fails → skip deduplication entirely
5. If auto-merge produces an invalid task → skip merge for that cluster, keep both tasks
6. If dedup history file is unwritable → skip persistence, log warning
7. Total dedup budget is 30s — if exceeded, return whatever is computed so far

---

## Integration Points with Existing Code

### Reuse from `semantic_cache.py`
- `_embed(text)` → TF-IDF word-frequency vectors
- `_cosine(v1, v2)` → cosine similarity between sparse vectors
- `_normalize(text)` → lowercase, strip punctuation, collapse whitespace

### Reuse from `embedding_scorer.py`
- `_load_model()` → sentence-transformers `gte-small` model loader
- `_encode_batch(texts)` → batch embedding computation
- Backend selection logic (TF-IDF fallback when model unavailable)

### Reuse from `duplicate_detector.py`
- `merge_duplicate_tasks(canonical, duplicate)` → field merge logic (title, description, priority, owned_files, completion_signals)
- Needs extension: current merge logic is Jaccard word-overlap; new workflow uses embedding similarity but same merge strategy

### New code needed
- `src/bernstein/core/task_dedup.py` — orchestration module:
  - `deduplicate_tasks(tasks: list[Task], config: DedupConfig) -> DeduplicationReport`
  - `DedupConfig` dataclass with thresholds, backend preference, auto_merge flag
  - `DuplicateCluster` and `DeduplicationReport` dataclasses
- Integration call sites:
  - `plan_loader.py` → `load_plan()`: call `deduplicate_tasks()` before returning
  - `planner.py` → `plan()`: call `deduplicate_tasks()` after LLM response parsing

---

## Relationship to Existing Deduplication

| Mechanism | Scope | Technique | Threshold | Status | Relationship to this workflow |
|---|---|---|---|---|---|
| `semantic_cache.py` (goal cache) | Planning goals | TF-IDF cosine | 0.85 | Active | Upstream — caches entire planning calls; this deduplicates individual tasks |
| `semantic_cache.py` (response cache) | Completed tasks | TF-IDF cosine | 0.95 | Active | Downstream — reuses results for tasks that reach execution; this prevents duplicates before execution |
| `duplicate_detector.py` | Task pairs | Jaccard word overlap | 0.70 | Unused | Overlapping — has merge logic we can reuse; similarity technique is lower fidelity |
| `workflow_importer.py` | Imported tasks | Exact title match | 1.0 | Active | Orthogonal — prevents re-import of TODO items; no semantic awareness |
| `request_dedup.py` | HTTP requests | Request ID hash | exact | Active | Orthogonal — HTTP idempotency; not task-level |
| **This workflow** | Plan tasks | Embedding cosine | 0.80/0.92 | Proposed | Fills the gap: semantic dedup at plan ingestion, before tasks reach the server |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Exact duplicate titles | Two tasks: "Add validation to UserController" × 2 | similarity ≥ 0.92; auto-merged into one task |
| TC-02: Semantic duplicate, different wording | "Add input validation to UserController" vs "Validate user input in UserController" | similarity ≥ 0.80; flagged as duplicate cluster |
| TC-03: Related but distinct tasks | "Refactor database queries" vs "Optimize slow database calls" | similarity 0.65-0.80; info-only, no merge |
| TC-04: Completely unrelated tasks | "Add login page" vs "Fix CSV export bug" | similarity < 0.65; not flagged |
| TC-05: Same description, different roles | "Add validation" (backend) vs "Add validation" (frontend) | Not compared (different roles); both kept |
| TC-06: Cross-stage duplicates | Stage 1 step 3 ≈ Stage 3 step 1 | Detected across stages; canonical from earlier stage |
| TC-07: Canonical selection — priority wins | Duplicate pair: priority=1 vs priority=2 | priority=1 task kept as canonical |
| TC-08: Canonical selection — earlier stage wins | Duplicate pair: same priority, stage 1 vs stage 3 | Stage 1 task kept as canonical |
| TC-09: Auto-merge enriches description | Canonical has short desc; duplicate has extra detail | Merged task has union of description content |
| TC-10: Auto-merge unions owned_files | Canonical owns `[a.py]`; duplicate owns `[b.py]` | Merged task owns `[a.py, b.py]` |
| TC-11: Warn cluster preserved | Similarity 0.85 (between thresholds) | Both tasks kept; metadata annotated; warning printed |
| TC-12: Empty plan | Plan with zero steps | Returns empty list; no dedup attempted |
| TC-13: Single task | Plan with one step | Returns single task; no pairwise comparison needed |
| TC-14: TF-IDF fallback | sentence-transformers not installed | Falls back to TF-IDF; dedup still works |
| TC-15: Total failure fallback | Both embedding backends fail | Returns original task list unchanged; logs warning |
| TC-16: Dedup history persistence | Successful dedup run | `.sdd/caching/dedup_history.jsonl` has new entry |
| TC-17: Large plan performance | Plan with 100 tasks | Completes within 30s budget; O(n²/2) = 4,950 comparisons |
| TC-18: Transitive closure | A~B (0.90) and B~C (0.88) but A~C (0.75) | All three in one cluster (transitive); canonical selected by priority/stage |
| TC-19: Dependency preservation | Duplicate task has `depends_on: [X]`; canonical has `depends_on: [Y]` | Merged task has `depends_on: [X, Y]` (union, deduplicated) |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Plans typically have 5-50 tasks; O(n²) pairwise comparison is acceptable | Verified: sample plans in `templates/plan.yaml` | Low — would need ANN index (FAISS/Annoy) for plans >200 tasks |
| A2 | TF-IDF cosine similarity is sufficient for detecting semantic duplicates in task titles/descriptions | Partially verified: `semantic_cache.py` uses it at 0.85 threshold with 30-50% hit rate | Medium — short titles with different vocabulary may score low despite being duplicates |
| A3 | `gte-small` model from `embedding_scorer.py` can be reused for task embedding | Verified: model is already loaded and used for file relevance scoring | Low |
| A4 | `duplicate_detector.py` merge logic correctly handles field combination | Not verified: module exists but is unused; may have edge cases | Medium — needs testing before relying on it |
| A5 | Auto-merge at 0.92 threshold won't produce false positives | Not verified: threshold is a guess | High — should be tuned using dedup history data after initial deployment; start conservative |
| A6 | Tasks with different roles are never semantic duplicates | Not always true — a "backend" and "fullstack" task may overlap | Medium — scope restriction may miss some duplicates; acceptable trade-off vs false positives |
| A7 | Description enrichment (appending unique sentences from duplicate) produces coherent task descriptions | Not verified | Medium — sentence boundary detection is imperfect; merged descriptions may read awkwardly |

---

## Open Questions

1. **Should auto-merge be opt-in?** Default behavior could be warn-only (safer), with `--dedup-auto-merge` flag to enable automatic merging. Recommendation: warn-only by default, auto-merge opt-in.
2. **Should dedup run on `POST /tasks/batch` server-side?** Currently proposed for plan_loader and planner only (client-side). Server-side would catch duplicates from all sources but adds latency to task creation. Recommendation: client-side first; server-side as follow-up if needed.
3. **How should the operator override a dedup decision?** Options: (a) `--no-dedup` flag to skip entirely, (b) per-task `dedup_exempt: true` field in plan YAML, (c) interactive prompt asking operator to confirm each warn-level cluster. Recommendation: (a) for now; (b) as enhancement.
4. **Should dedup history be used to tune thresholds?** After accumulating data, we could analyze false positive/negative rates and auto-adjust thresholds. Recommendation: yes, but as a separate follow-up task.
5. **What about plans with >200 tasks?** O(n²) becomes expensive. Options: (a) approximate nearest neighbor index (FAISS), (b) pre-filter by role to reduce comparison space, (c) accept the latency. Recommendation: (b) is already in the spec (role restriction); (a) if real-world plans exceed 200 tasks.
6. **Should duplicate detection consider task `owned_files` overlap?** Two tasks touching the same files are more likely to be duplicates. Could be used as a signal boost (multiply similarity by 1.1 if files overlap). Recommendation: worth exploring but not in v1.

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-11 | Initial spec created based on codebase discovery | — |
| 2026-04-11 | `duplicate_detector.py` exists with Jaccard similarity + merge logic but is completely unused | Documented; spec reuses merge logic but replaces similarity technique with embeddings |
| 2026-04-11 | `semantic_cache.py` has `_embed()` and `_cosine()` TF-IDF functions ready for reuse | Documented; spec reuses these as the zero-dep embedding backend |
| 2026-04-11 | `embedding_scorer.py` has `gte-small` model loader ready for reuse | Documented; spec reuses as the enhanced embedding backend |
| 2026-04-11 | `plan_loader.py` has no deduplication at all — returns raw parsed tasks | Documented; Step 6 integration point |
| 2026-04-11 | `planner.py` fetches existing task titles to avoid known duplicates (exact match) but does not check within its own output | Documented; Step 6 integration point |
| 2026-04-11 | Response cache in `semantic_cache.py` operates at 0.95 threshold — much higher than proposed dedup thresholds | Documented; different use case (result reuse vs duplicate detection) justifies different thresholds |

---

## Implementation Priority

Recommended implementation order:

1. **Core deduplicator** (`task_dedup.py`) — dataclasses, embedding call, pairwise similarity, clustering, report generation
2. **TF-IDF backend integration** — reuse `_embed()` and `_cosine()` from `semantic_cache.py`
3. **Plan loader integration** — call `deduplicate_tasks()` in `load_plan()` return path
4. **Planner integration** — call `deduplicate_tasks()` after LLM response parsing
5. **Dedup history persistence** — append to `.sdd/caching/dedup_history.jsonl`
6. **sentence-transformers backend** — reuse `embedding_scorer.py` model loader for higher accuracy
7. **CLI flag** — `--dedup-auto-merge` / `--no-dedup` for operator control

Steps 1-4 are the MVP. Steps 5-7 are enhancements.
