## Anchor audit checkpoints outside the local filesystem

Adds optional external anchoring of audit checkpoints using RFC 3161 TimeStampTokens, enabling verification that local audit history hasn’t been truncated or tampered. New CLI commands:

- `bernstein audit anchor --print-request` – prints a SHA-256 digest and an `openssl ts -query` command for TSA preparation
- `bernstein audit anchor --rfc3161-token <file>` – records an RFC 3161 token alongside an audit checkpoint
- `bernstein audit anchor --rfc3161-tsa-url <url>` – stores a TSA URL reference

`bernstein doctor` also surfaces anchoring posture with a new "Audit anchoring" row.

(#5147)