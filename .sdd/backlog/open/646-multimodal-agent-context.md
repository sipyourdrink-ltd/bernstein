# 646 — Multimodal Agent Context

**Role:** backend
**Priority:** 5 (low)
**Scope:** large
**Depends on:** none

## Problem

Agents can only receive text-based context. Screenshots, UI mockups, architecture diagrams, and design specs in visual format cannot be passed to agents. 80% of foundation models will be multimodal by 2028. For UI-related coding tasks, visual context dramatically improves accuracy.

## Design

Add support for multimodal inputs (screenshots, diagrams, images) in agent context. Extend the task specification format to include image attachments: file paths or URLs to visual assets. The spawner passes images to the agent adapter, which includes them in the agent's context using the underlying model's multimodal API. Support common formats: PNG, JPEG, SVG, and PDF (first page). Implement image preprocessing: resize large images to reduce token cost, extract text from screenshots via OCR as a fallback for text-only models. Store image references in the task spec, not the images themselves. Add a `--screenshot` flag to `bernstein run` that captures the current screen and includes it as context. Define which adapters support multimodal input and gracefully degrade for those that don't (include OCR text instead).

## Files to modify

- `src/bernstein/core/multimodal.py` (new)
- `src/bernstein/core/spawner.py`
- `src/bernstein/adapters/base.py`
- `src/bernstein/adapters/claude.py`
- `tests/unit/test_multimodal.py` (new)

## Completion signal

- Tasks can include image attachments in their specification
- Claude adapter passes images to the agent as multimodal context
- Fallback to OCR text for adapters that don't support images
