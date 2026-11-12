# Context policy and receipt fingerprinting

## Overview

Every agent run produces a **context receipt** that fingerprints each piece of
context (role, tasks, examples, etc.) sent to the LLM. The receipt enables
deterministic replay by detecting if context has drifted between runs.

A context receipt records:
- **One entry per context section** with its SHA-256 content hash
- **Token and character estimates** for sizing and accounting
- **Policy metadata** (policy_id, policy_version) that generated the receipt
- **Totals** across all sections for fast verification

## Configuration

Context policy is configured in `bernstein.yaml` under the top-level `context`
key. All fields are optional.

```yaml
context:
  policy_id: "default"
  policy_version: "1"
  scope_to_agents_md: false
```

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `policy_id` | string | `"default"` | Logical identifier for the policy — use to distinguish runs with different context structures |
| `policy_version` | string | `"1"` | Schema version — bump when context structure evolves |
| `scope_to_agents_md` | boolean | `false` | When true, only documented context sections (in `.sdd/agents-md/`) are included in the receipt |

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
    }
  ],
  "total_token_estimate": 450,
  "total_chars": 3200,
  "section_count": 1
}
```

Each **entry** corresponds to one context section included in the agent prompt:

- `label`: section name (`"role"`, `"tasks"`, `"examples"`, `"lessons"`, etc.)
- `content_sha256`: SHA-256 hex digest of the section content (with `sha256:` prefix)
- `token_estimate`: estimated token count
- `char_count`: raw UTF-8 character count

## Deterministic replay

When replaying a run:

1. **Recompute** the context receipt from the current on-disk context sections
2. **Compare hashes** — each `content_sha256` must match the recorded hash
3. **Compare totals** — `total_token_estimate`, `total_chars`, `section_count`
   must match
4. **Check policy** — `policy_id` and `policy_version` must match

A mismatch indicates context drift. Strict replay mode refuses to proceed;
audit mode flags the divergence with a named list of changed fields.

## Scope to agents-md

When `scope_to_agents_md: true`, only context sections declared in
`.sdd/agents-md/` are included in the receipt. Undocumented sections are
excluded. This enforces that every context section has a corresponding
documentation entry, making the context structure auditable.

## Storage and verification

- **On-disk**: receipts are stored in the run journal and audit chain
- **Verification**: `bernstein context verify --task-id <id>` recomputes the
  receipt and checks for drift
- **Replay**: deterministic replay uses the receipt to detect context changes
  and refuses or flags according to policy

## Related documentation

- [Context policy configuration](../operations/context-policy.md) — operator
  guide to configuring context policy
- [Context receipt schema](../reference/context-receipt.md) — complete schema
  reference
- [Deterministic replay](../operations/deterministic-replay.md) — how receipts
  enable reproducible runs
