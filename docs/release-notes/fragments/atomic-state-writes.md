## Trigger state and fleet contexts are written crash-safely

Two runtime stores had a local `_atomic_write` helper that renamed a temporary file into place without ever calling `fsync`, so a power loss could make the rename durable while the bytes behind it were not.

For `.sdd/runtime/triggers/`, what survives that is a zero-length `counters.json`, and the store's own loader answers an unparseable state file by quarantining it and raising `TriggerStateCorruptError`.

For `.sdd/fleet/contexts/`, what survives is a truncated `active.json`, which `_active_field` reads as "no context active": the fleet silently falls back to four-layer precedence while the audit chain still records the activation. That writer also used one fixed temporary name per target with no lock around it, so two concurrent activations could write the same temporary and publish a mix of both.

Both now go through `write_atomic_bytes` in `core/persistence/atomic_write.py`, the module that already centralises this pattern: a per-writer temporary name, `fsync` of the file, `fsync` of the containing directory, and owner-only permissions on runtime state.
