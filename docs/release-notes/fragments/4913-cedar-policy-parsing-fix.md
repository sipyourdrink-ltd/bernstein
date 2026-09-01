### Fixed

- **CedarHook** now parses whole policy statements (to the terminating `;`), not line-by-line. Conventional multi-line Cedar formatting like:
  ```
  permit
    principal,
    action == "bash",
    resource;
  ```
  is now read correctly instead of yielding ABSTAIN for every request.

- **Unsupported constructs are rejected at construction** rather than being silently dropped. Policies containing `when`, `unless`, `?principal` slots, or resource/principal constraints now raise `ValueError` with the problematic statement and construct name. This prevents silent mis-evaluation where conditions were ignored and ALLOW was returned unconditionally.

- **Policy digest is recorded** with each verdict (`HookResponse.policy_digest`). The SHA-256 digest of the exact policy text ties every ALLOW/DENY/ABSTAIN back to the policy bytes that produced it.

([#4913](https://github.com/sipyourdrink-ltd/bernstein/issues/4913))