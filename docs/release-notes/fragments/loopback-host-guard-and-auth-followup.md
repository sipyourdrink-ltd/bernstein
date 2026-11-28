## Loopback host allowlist and token-file hardening

Loopback task-server rejections for non-loopback `Host` headers now name
`BERNSTEIN_ALLOWED_HOSTS` directly in the HTTP `403` detail payload and CLI
reference so reverse-proxy and container-alias deployments can discover the
allowlist variable immediately. Persisted `.sdd/runtime/auth.token` reuse also
validates via `lstat`/`fstat` that the token file is a regular file with
owner-only permissions and matching UID before reuse, rotating unsafe files.
