# F91 — Multimodal Agent Support

**Priority:** P5
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 5 — Future-Proofing 2030-2035

## Problem
Agents are limited to text-only inputs and outputs, preventing orchestration of tasks involving images, audio, or video that modern AI models increasingly support.

## Solution
- Extend the agent adapter interface with a `capabilities` field: `capabilities: [text, image, audio, video]`
- Agents declare supported modalities during registration
- Task definitions gain an `input_modalities` field specifying required input types
- Router matches tasks to agents based on capability compatibility
- Define serialization format for multimodal payloads: base64-encoded with MIME type headers
- Extend verification protocol to handle non-text outputs (image diff, audio transcript comparison)
- Add `bernstein agents capabilities <name>` CLI to inspect an agent's supported modalities

## Acceptance
- [ ] Agent adapter interface includes `capabilities` field for modality declaration
- [ ] Task definitions support `input_modalities` field
- [ ] Router selects agents based on capability match with task requirements
- [ ] Multimodal payloads serialized with base64 encoding and MIME type headers
- [ ] Verification protocol handles image and audio outputs
- [ ] `bernstein agents capabilities <name>` displays agent modality support
