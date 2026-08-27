# Context receipt schema

The **context receipt** is a content-addressed record of the exact context
sections sent to an agent's LLM during a run. It enables deterministic replay by
letting verifiers detect when context has drifted between runs.

## Overview

A context receipt is embedded in every run's journal and audit chain. It
contains:

- **One entry per context section** (role, tasks, examples, lessons, etc.)
- **Content hash** (SHA-256) of each section
- **Token and character estimates** for each section
- **Totals** across all sections

## Receipt structure

```json
{
  "policy": {
    "policy_id": "default",
    "policy_version": "1"
  },
  "entries": [
    {
      "label": "role",
      "content_sha256": "sha256:abc123...",
      "token_estimate": 450,
      "char_count": 3200
    },
    {
      "label": "tasks",
      "content_sha256": "sha256:def456...",
      "token_estimate": 120,
      "char_count": 850
    }
  ],
  "total_token_estimate": 570,
  "total_chars": 4050,
  "section_count": 2
}
```

### Top-level fields

| Field | Type | Description |
|-------|------|-------------|
| `policy` | object | Policy configuration that generated this receipt (see [Policy](#policy)) |
| `entries` | array | One entry per context section, in the order they were sent to the model |
| `total_token_estimate` | integer | Sum of all `entry.token_estimate` values |
| `total_chars` | integer | Sum of all `entry.char_count` values |
| `section_count` | integer | Number of entries in the `entries` array |

### `policy`

The policy configuration that was active when this receipt was generated.

| Field | Type | Description |
|-------|------|-------------|
| `policy_id` | string | Logical identifier for the policy (e.g., `"default"`, `"enhanced"`) |
| `policy_version` | string | Version of the policy schema (e.g., `"1"`) |

### `entries`

Each entry corresponds to one context section that was included in the agent's
prompt. The sections appear in the order they were constructed.

| Field | Type | Description |
|-------|------|-------------|
| `label` | string | Section name (e.g., `"role"`, `"tasks"`, `"examples"`, `"lessons"`) |
| `content_sha256` | string | SHA-256 hex digest of the section content (prefixed with `sha256:`) |
| `token_estimate` | integer | Estimated token count, computed via :func:`estimate_tokens_for_text` |
| `char_count` | integer | Raw UTF-8 character count of the section content |

## Policy

The `policy` object records which context policy was used to generate the
receipt. This allows verifiers to detect when the policy itself has changed.

### Policy fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `policy_id` | string | `"default"` | Logical identifier for the policy |
| `policy_version` | string | `"1"` | Version of the policy schema |

See [Context policy configuration](../operations/context-policy.md) for details
on configuring policy in `bernstein.yaml`.

## Example receipt

Here's a complete example from a real run with three context sections:

```json
{
  "policy": {
    "policy_id": "default",
    "policy_version": "1"
  },
  "entries": [
    {
      "label": "role",
      "content_sha256": "sha256:7f82b3c4e1a09f5d8c6b2a4e1d0f3c5a7b9e2f1d4c6b8a0e2f4d6c8b0a2e4f6d",
      "token_estimate": 285,
      "char_count": 2048
    },
    {
      "label": "tasks",
      "content_sha256": "sha256:3a5b7c9e1f2d4a6b8c0e2f4d6a8b0c2e4f6d8a0b2c4e6f8a0b2d4f6a8c0e2f4d",
      "token_estimate": 42,
      "char_count": 312
    },
    {
      "label": "lessons",
      "content_sha256": "sha256:9b1c3d5e7f8a2b4c6d8e0f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2d",
      "token_estimate": 18,
      "char_count": 128
    }
  ],
  "total_token_estimate": 345,
  "total_chars": 2488,
  "section_count": 3
}
```

## Using the receipt for verification

To verify that a run's context hasn't changed:

1. **Recompute** the context receipt from the on-disk context sections
2. **Compare hashes** - each `content_sha256` in the recomputed receipt must
   match the recorded hash
3. **Compare totals** - `total_token_estimate`, `total_chars`, and
   `section_count` must match
4. **Check policy** - the `policy_id` and `policy_version` must match the
   current configuration

If any field differs, the context has drifted and deterministic replay should
refuse to proceed.

## Related topics

- [Context policy configuration](../operations/context-policy.md) — How to
  configure policy in `bernstein.yaml`
- [Deterministic replay](../operations/deterministic-replay.md) — How context
  receipts enable reproducible runs
- [Run receipts](run-receipts.md) — Overview of run-level receipts and
  attestation
