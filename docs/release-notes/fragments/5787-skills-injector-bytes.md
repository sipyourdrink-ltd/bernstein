## Injected skills are written as the bytes their audit digest covers

On Windows, text-mode file writes translated `\n` to `\r\n`, causing on-disk
bytes in `.claude/skills/` to diverge from the BLAKE2b `rendered_digest` recorded
in the audit log.

`inject_skills` now encodes the rendered skill once, computes the digest over
those UTF-8 bytes, and writes the exact same bytes using binary mode
(`write_bytes`). The on-disk file matches the audit digest byte-for-byte on all
platforms.
