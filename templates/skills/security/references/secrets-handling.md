# Secrets handling

## Storage
- Environment variables for runtime secrets; `.env` files are developer-only.
- Secrets-manager for long-lived credentials (Vault, AWS Secrets Manager).
- Never commit secrets — audit with `ggshield` / `trufflehog` before push.

## Rotation
- Scheduled rotation for IAM credentials, database passwords, API tokens.
- Rotation must not require downtime; use dual-keying.

## Access
- Principle of least privilege for per-agent scoping
  (`src/bernstein/core/credential_scoping.py`).
- Audit who read each secret; store the event in the HMAC audit log.

## In-code hygiene
- Redact secrets from logs and exception messages.
- `repr()` of config objects must drop sensitive fields (use Pydantic
  `SecretStr`).
- Cryptographic nonces come from `secrets.token_bytes`, never `random`.
