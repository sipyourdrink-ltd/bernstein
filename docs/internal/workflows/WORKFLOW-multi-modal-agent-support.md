# WORKFLOW: Multi-Modal Agent Support
**Version**: 0.1
**Date**: 2026-04-11
**Author**: Workflow Architect
**Status**: Draft
**Implements**: road-184-multi-modal-agent-support

---

## Overview

Extend the task-to-agent pipeline so that tasks can carry multi-modal attachments — architecture diagrams, UI mockups, data flow diagrams — alongside the text prompt. Attachments flow from task creation (API or plan YAML) through spawn prompt rendering to CLI adapter invocation, where the underlying agent (Claude, Gemini, etc.) receives both the text prompt and the visual context needed to implement from a diagram.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Plan Author | Declares `attachments:` in plan YAML steps |
| API Caller | Submits tasks with attachments via `POST /tasks` |
| Plan Loader | Parses `attachments:` from step YAML into Task objects |
| Task Server (Starlette) | Validates and persists tasks with attachment metadata |
| TaskStore | Persists attachment metadata in JSONL (paths, not binary) |
| Spawner (`AgentSpawner`) | Passes attachment metadata to spawn prompt renderer |
| Spawn Prompt Renderer | Injects attachment references into the agent's text prompt |
| CLI Adapter (base) | Receives attachment metadata alongside the prompt |
| Claude Adapter | Passes image paths via agent's `Read` tool reference or `--image` flag |
| Gemini Adapter | Passes image paths via `--image` flag (native support) |
| Codex Adapter | Text-only fallback — embeds path references, no native vision |
| Spawned Agent | Reads referenced files via its own tools (Read, etc.) |

---

## Prerequisites

- Attachment files must exist on the local filesystem accessible to the spawned agent's worktree
- For image inputs: the underlying model must support vision (Claude 3+, Gemini Pro Vision+)
- Adapter must declare its multi-modal capability level (see Step 2)

---

## Trigger

Three entry points can initiate a multi-modal task:

1. **Plan YAML** — step with `attachments:` field
2. **API** — `POST /tasks` with `attachments` in request body
3. **Programmatic** — `TaskStore.create()` with `attachments` parameter

---

## Workflow Tree

### STEP 1: Task creation with attachments

**Actor**: Plan Loader / API Server / TaskStore
**Action**: Accept attachment metadata as part of task creation. Validate each attachment exists and has a supported media type.

**Input — Plan YAML format**:
```yaml
steps:
  - title: "Implement auth flow from diagram"
    role: backend
    attachments:
      - path: docs/diagrams/auth-flow.png
        media_type: image/png
        description: "Authentication flow diagram — implement all branches"
      - path: docs/architecture/system-overview.svg
        media_type: image/svg+xml
        description: "System architecture — locate the auth service"
```

**Input — API format**:
```json
{
  "title": "Implement auth flow from diagram",
  "role": "backend",
  "attachments": [
    {
      "path": "docs/diagrams/auth-flow.png",
      "media_type": "image/png",
      "description": "Authentication flow diagram"
    }
  ]
}
```

**Timeout**: 2s (validation + filesystem stat)

**Output on SUCCESS**: Task record created with `attachments` field populated → GO TO STEP 2

**Output on FAILURE**:
  - `FAILURE(file_not_found)`: Attachment path does not exist → reject task creation with 400 + message listing missing files. Do not create a partial task.
  - `FAILURE(unsupported_media_type)`: File extension/type not in supported set → reject with 400 + message listing unsupported types.
  - `FAILURE(file_too_large)`: Attachment exceeds size limit (e.g., >20MB) → reject with 400 + size limit info.
  - `FAILURE(validation_error)`: Missing required fields (path, media_type) → reject with 400 + field errors.

**Observable states**:
  - Caller sees: 201 Created with task ID, or 400 with validation errors
  - Database: Task record in JSONL includes `"attachments": [...]` metadata
  - Logs: `[task_store] task {id} created with {n} attachments`

**Supported media types** (initial set):
| Category | Media types | Use case |
|---|---|---|
| Images | `image/png`, `image/jpeg`, `image/webp`, `image/gif` | UI mockups, screenshots, diagrams |
| Vector | `image/svg+xml` | Architecture diagrams, flow charts |
| Documents | `application/pdf` | Architecture docs, specs |

**REALITY CHECK FINDING RC-1**: `Task` dataclass (`models.py:226-280`) has no `attachments` field. `TaskCreateRequest` Protocol (`task_store.py:130-173`) has no `attachments` property. `TaskCreate` Pydantic model (`server.py:277-307`) has no `attachments` field. `TaskRecord` TypedDict (`task_store.py:48-100`) has no `attachments` key. All four must be extended.

**REALITY CHECK FINDING RC-2**: `PlanConfig` has `context_files: list[str]` (`plan_loader.py:63`) but this field is never propagated to individual `Task` objects. The infrastructure for plan-level file references exists but is disconnected from the task pipeline.

---

### STEP 2: Capability detection

**Actor**: Spawner (`AgentSpawner`)
**Action**: Before spawning, check whether the selected adapter supports multi-modal input. This determines how attachments are rendered in the prompt.
**Timeout**: <1ms (in-memory lookup)

**Input**: `adapter.name`, `model_config.model`, task attachments list
**Output on SUCCESS**: Capability level determined → GO TO STEP 3

**Capability levels**:
| Level | Meaning | Adapters |
|---|---|---|
| `native_vision` | Adapter can pass images directly to the model via CLI flags | Claude (via Read tool), Gemini (via `--image`) |
| `path_reference` | Adapter can reference file paths; agent reads them with tools | Claude (always works), any adapter with file-read tools |
| `text_only` | Adapter cannot handle images — only text description is injected | Codex, Aider, generic adapters without vision |

**Output on FAILURE**:
  - `FAILURE(adapter_unknown)`: Adapter not in capability registry → default to `text_only`, log warning

**REALITY CHECK FINDING RC-3**: No adapter declares multi-modal capability. The `CLIAdapter` base class (`base.py:141-153`) has no `supports_vision` property or method. `ModelConfig` (`models.py:417-424`) has no `supports_vision: bool` field. Both need a capability declaration mechanism.

**Observable states**:
  - Operator sees: `[spawner] adapter {name} capability: {level} for {n} attachments`

---

### STEP 3: Spawn prompt rendering with attachments

**Actor**: Spawn Prompt Renderer (`spawn_prompt.py._render_prompt`)
**Action**: Inject attachment context into the prompt based on the capability level determined in Step 2.
**Timeout**: 2s (prompt assembly)

**Input**: Task with attachments, capability level, existing prompt sections
**Output on SUCCESS**: Rendered prompt string with attachment context → GO TO STEP 4

**Rendering strategy by capability level**:

**`native_vision`** — inject file paths with instructions for the agent to read them:
```markdown
## Attached Visual Context

The following files contain visual context for this task. Use your Read tool
to view each image before starting implementation.

| # | File | Type | Description |
|---|---|---|---|
| 1 | /worktree/docs/diagrams/auth-flow.png | image/png | Authentication flow diagram |
| 2 | /worktree/docs/architecture/overview.svg | image/svg+xml | System architecture |

IMPORTANT: Read and analyze ALL attached images before writing any code.
These diagrams define the required behavior — implement what they show.
```

**`path_reference`** — same as above but with explicit tool instructions:
```markdown
## Attached Visual Context

Read these files using your file-reading tools:
- /worktree/docs/diagrams/auth-flow.png — Authentication flow diagram
- /worktree/docs/architecture/overview.svg — System architecture

If you cannot read image files directly, describe what you expect the diagram
to show based on the file name and description, and proceed with implementation.
```

**`text_only`** — inject only the text descriptions:
```markdown
## Visual Context (descriptions only — images not available to this agent)

1. **Authentication flow diagram** (docs/diagrams/auth-flow.png):
   Diagram showing the authentication flow. Implement all branches shown.

2. **System architecture** (docs/architecture/overview.svg):
   Architecture diagram showing the system overview. Locate the auth service.

NOTE: The original images are not available to your model. Work from these
descriptions and the existing codebase.
```

**Output on FAILURE**:
  - `FAILURE(file_moved)`: Attachment path existed at task creation but not at spawn time (file was deleted/moved between task creation and spawn) → log warning, render as `text_only` with note that image is unavailable. Do not fail the spawn — degrade gracefully.
  - `FAILURE(prompt_too_large)`: Adding attachment context exceeds prompt size budget → truncate attachment descriptions (not paths), log warning, continue with truncated context.

**REALITY CHECK FINDING RC-4**: `_render_prompt()` (`spawn_prompt.py:527-685`) has no attachment handling section. RAG context injection (`spawn_prompt.py:568-583`) reads files via `path.read_text()` which fails or produces garbage for binary image files. Attachment rendering must be a distinct section that does NOT attempt to read binary file contents into the prompt.

**Observable states**:
  - Logs: `[spawn_prompt] rendered {n} attachments at capability level {level}`

---

### STEP 4: Worktree preparation

**Actor**: Spawner
**Action**: When creating the agent's git worktree, ensure attachment files are accessible. Attachment paths in the task are relative to the project root. The worktree must either contain these files (if they're tracked in git) or have symlinks/copies to them.
**Timeout**: 30s (worktree creation, same as existing)

**Input**: Worktree path, attachment paths (relative)
**Output on SUCCESS**: All attachment files accessible at resolved paths in worktree → GO TO STEP 5

**Output on FAILURE**:
  - `FAILURE(file_not_in_worktree)`: Attachment file is not tracked in git and doesn't exist in worktree → copy file from main worktree to agent worktree, log info
  - `FAILURE(copy_failed)`: Cannot copy untracked file → degrade to `text_only` for that attachment, log warning, continue spawn

**Observable states**:
  - Logs: `[spawner] verified {n}/{total} attachments accessible in worktree {path}`

**REALITY CHECK FINDING RC-5**: Current worktree creation (`spawner.py:1267-1310`) does not consider non-git-tracked files. If diagrams are in `.gitignore`d directories or outside the repo, they won't be in the worktree. The spawner must handle this gap.

---

### STEP 5: Adapter invocation

**Actor**: CLI Adapter
**Action**: Spawn the agent with both the text prompt and attachment context. Adapter-specific behavior:
**Timeout**: Existing adapter timeout (model_config dependent)

**Claude adapter** (`claude.py`):
- Attachments are referenced by path in the prompt text (rendered in Step 3)
- The Claude Code agent natively supports reading images via its `Read` tool
- No CLI flag changes needed — the agent reads the files itself
- Optional enhancement: `--append-system-prompt` could include "You have image attachments — use Read tool to view them"

**Gemini adapter** (`gemini.py`):
- Gemini CLI supports `--image <path>` for direct image input
- For each image attachment: add `--image <worktree_path>` to the command
- Non-image attachments (PDF, SVG): reference by path in prompt text

**Codex adapter** (`codex.py`):
- No native vision support
- Prompt includes text descriptions only (Step 3, `text_only` rendering)

**Input**: `prompt: str` (with attachment context rendered in Step 3), `attachments: list[Attachment] | None`
**Output on SUCCESS**: `SpawnResult` with pid, log_path → normal agent lifecycle continues

**Output on FAILURE**:
  - `FAILURE(spawn_error)`: Agent process fails to start → existing spawn failure handling applies
  - `FAILURE(image_flag_unsupported)`: Adapter CLI doesn't support `--image` flag → fall back to path-reference in prompt, log warning, re-spawn

**Observable states**:
  - Operator sees: agent spawned with `{n} attachments` in spawn log
  - Logs: `[adapter] spawned {agent_id} with {n} attachments at capability {level}`

**REALITY CHECK FINDING RC-6**: `CLIAdapter.spawn()` signature (`base.py:141-153`) has no `attachments` parameter. Adding it as optional (`attachments: list[Attachment] | None = None`) preserves backward compatibility. Adapters that don't override the parameter ignore it.

**REALITY CHECK FINDING RC-7**: The Claude adapter's `_build_command()` (`claude.py:236-287`) has no `--image` flag and no mechanism to pass binary data. However, this is less critical because Claude Code agents can read image files via their `Read` tool — the prompt-based path reference approach (Step 3, `native_vision`) works without adapter changes.

---

### STEP 6: Agent processes attachments

**Actor**: Spawned Agent (Claude, Gemini, etc.)
**Action**: Agent reads the attachment files referenced in its prompt, analyzes the visual content, and uses it to inform implementation.
**Timeout**: Agent-level timeout (existing)

This step is entirely within the agent's control — Bernstein does not observe the agent's internal tool usage. The workflow's responsibility ends at providing the attachments in the prompt.

**Output on SUCCESS**: Agent completes task with implementation informed by visual context → normal task completion
**Output on FAILURE**:
  - `FAILURE(cannot_read_image)`: Agent's model doesn't support vision or tool can't read the file format → agent proceeds with text context only (graceful degradation within agent)
  - `FAILURE(misinterpretation)`: Agent misreads diagram → detected at quality gate / review stage (out of scope for this workflow)

**Observable states**:
  - Customer sees: N/A (developer workflow)
  - Operator sees: agent task status transitions normally
  - Logs: agent's own stdout/stderr (adapter-dependent streaming)

---

## State Transitions

```
[task_created_with_attachments]
  → (attachments validated) → [task_open]
  → (attachments invalid) → [task_rejected] (never created)

[task_open]
  → (claimed by spawner) → [task_claimed]

[task_claimed]
  → (capability detected, prompt rendered, worktree prepared) → [agent_spawning]
  → (all attachments inaccessible + text_only fallback) → [agent_spawning_degraded]

[agent_spawning]
  → (agent starts successfully) → [agent_running]
  → (spawn fails) → [task_failed] (existing failure path)

[agent_running]
  → (agent completes) → [task_done]
  → (agent fails) → [task_failed]
```

---

## Handoff Contracts

### Plan YAML → Plan Loader

**File format**: YAML with `attachments:` array per step
**Payload per attachment**:
```yaml
path: "relative/path/to/file.png"     # required, relative to project root
media_type: "image/png"                # required, MIME type
description: "What this shows"         # required, used as fallback for text-only
```
**Validation**: Plan Loader checks file existence at parse time. Missing files → `PlanLoadError`.

### API Caller → Task Server

**Endpoint**: `POST /tasks`
**Payload** (attachment portion):
```json
{
  "attachments": [
    {
      "path": "relative/path/to/file.png",
      "media_type": "image/png",
      "description": "What this shows"
    }
  ]
}
```
**Success response**: `201 Created` with task ID
**Failure response**:
```json
{
  "ok": false,
  "error": "Attachment file not found: docs/missing.png",
  "code": "ATTACHMENT_NOT_FOUND",
  "retryable": false
}
```
**Timeout**: 5s

### Spawner → Adapter

**Method**: `adapter.spawn(prompt=..., workdir=..., model_config=..., session_id=..., attachments=...)`
**Payload**: `attachments: list[Attachment] | None` — metadata only, not binary content
**Attachment schema**:
```python
@dataclass(frozen=True)
class Attachment:
    path: str           # absolute path in worktree
    media_type: str     # MIME type
    description: str    # human-readable description
```
**Success response**: `SpawnResult` (existing)
**Failure response**: `SpawnResult` with `abort_reason` set (existing)

### Spawner → Spawn Prompt Renderer

**Method**: `_render_prompt(tasks, ..., attachments=...)` or via `task.attachments`
**Input**: List of attachments with resolved worktree paths
**Output**: Prompt string with `## Attached Visual Context` section inserted

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Copied attachment files in worktree | Step 4 | Worktree cleanup (existing janitor) | `rm -rf` worktree directory |
| Task record with attachments | Step 1 | Normal task lifecycle | JSONL archival |

No new persistent resources are created beyond what the existing task lifecycle manages.

---

## Reality Checker Findings Summary

| # | Finding | Severity | Spec section | Resolution |
|---|---|---|---|---|
| RC-1 | `Task`, `TaskCreateRequest`, `TaskCreate`, `TaskRecord` all lack `attachments` field | Critical | Step 1 | Add `attachments` field to all four types |
| RC-2 | `PlanConfig.context_files` exists but is disconnected from Task pipeline | Medium | Step 1 | Either extend `context_files` propagation or add separate `attachments` |
| RC-3 | No adapter declares multi-modal capability; no `supports_vision` property | High | Step 2 | Add capability declaration to base adapter |
| RC-4 | `_render_prompt` has no attachment section; RAG reads binary files as text | High | Step 3 | Add attachment rendering section, skip binary in RAG |
| RC-5 | Worktree creation doesn't handle non-git-tracked attachment files | Medium | Step 4 | Copy untracked attachments to worktree |
| RC-6 | `CLIAdapter.spawn()` has no `attachments` parameter | High | Step 5 | Add optional parameter to base class |
| RC-7 | Claude adapter has no `--image` flag, but Read tool works as alternative | Low | Step 5 | Prompt-based path reference is sufficient for Claude |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — image attachment with Claude | Task with PNG attachment, Claude adapter | Agent receives prompt with image path, reads and analyzes it |
| TC-02: Happy path — image with Gemini | Task with PNG, Gemini adapter | `--image` flag added to Gemini CLI command |
| TC-03: Text-only fallback | Task with PNG, Codex adapter | Prompt contains text description only, no image flag |
| TC-04: Missing attachment at creation | `POST /tasks` with path to nonexistent file | 400 error with `ATTACHMENT_NOT_FOUND` |
| TC-05: Unsupported media type | Attachment with `video/mp4` | 400 error with `UNSUPPORTED_MEDIA_TYPE` |
| TC-06: File too large | Attachment >20MB | 400 error with `ATTACHMENT_TOO_LARGE` |
| TC-07: File deleted between creation and spawn | File exists at creation, removed before spawn | Degraded to text-only for that attachment, spawn succeeds |
| TC-08: Untracked file in worktree | Attachment in `.gitignore`d directory | File copied to worktree, spawn succeeds |
| TC-09: Copy fails | Untracked file, permission denied on copy | Degraded to text-only, spawn succeeds |
| TC-10: Plan YAML with attachments | Plan step has `attachments:` list | Parsed correctly, files validated, task created |
| TC-11: Multiple attachments | Task with 3 images + 1 PDF | All rendered in prompt, all accessible in worktree |
| TC-12: Prompt size limit | 10 large images push prompt past budget | Descriptions truncated, paths preserved, warning logged |
| TC-13: No attachments (backward compat) | Task created without `attachments` field | Existing behavior unchanged, no errors |
| TC-14: SVG attachment | Architecture diagram in SVG format | Handled as image, referenced by path |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Claude Code agents can read image files via their `Read` tool | Verified: Claude Code documentation | Would need `--image` CLI flag instead |
| A2 | Gemini CLI supports `--image` flag | Not verified against current CLI version | Would fall back to path-reference approach |
| A3 | Attachment files are small enough to fit in agent context | Not verified | Large diagrams could exceed model context window |
| A4 | Worktree cleanup (janitor) handles copied attachment files | Verified: janitor removes entire worktree directory | Orphaned files if janitor fails |
| A5 | All attachment paths are relative to project root | Design decision | Absolute paths would bypass worktree scoping |
| A6 | Binary file content is never injected into the prompt text | Design decision | Would corrupt the prompt if violated |
| A7 | Models that don't support vision still produce useful output from text descriptions | Assumed | Lower quality output for text-only fallback |

## Open Questions

- Should Bernstein validate that the selected model actually supports vision before spawning with image attachments? Currently this is left to graceful degradation.
- Should attachment files be copied into the worktree unconditionally (for isolation) or symlinked (for disk savings)?
- Should there be a maximum number of attachments per task?
- Should the system support URL-based attachments (fetch from remote) in addition to local paths?
- For PDF attachments: should specific page ranges be extractable in the attachment spec?
- Should the `Attachment` type support inline base64 content for small images, or always require file paths?
- Should the quality gate include a "visual verification" step where a reviewer agent checks implementation against the diagram?

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-11 | Initial spec created with 7 Reality Checker findings | — |
| 2026-04-11 | Confirmed no multi-modal data path exists end-to-end | Documented full pipeline gap analysis |
