# Edge-case checklist (Bernstein)

When writing a new test suite, walk through this list and cover what's
relevant:

- Empty input (`[]`, `""`, `None`).
- Single-element collections.
- Boundary values (off-by-one, `0`, `-1`, `MAX_INT`).
- Unicode, non-ASCII, and RTL scripts in text fields.
- Timezones — naive datetimes vs. aware UTC.
- Paths with spaces, unicode, and traversal (`../`) attempts.
- Concurrent access — two calls racing on the same resource.
- Large inputs — 1 MB strings, 10 000-item lists.
- Network errors — timeout, refused, partial read, malformed JSON.
- Partial failures — half the batch succeeds, half fails.
- Idempotency — retrying the same operation twice.
- Permissions — the caller is missing one specific scope.
