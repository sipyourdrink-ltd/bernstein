## `trace follow` adds resume, file export, and live following

`bernstein trace follow <entity-id>` now supports `--since <entry-id>`, `--out <path>`, and `--live` (`-f`):

- `--since <entry-id>` resumes follow output strictly after the specified journal entry id, matching both trace and ledger identifiers (`trace:<id>`, `<id>`, `ledger:<run>:<seq>`, `<run>:<seq>`, a trace's `sha256`, and a ledger entry's `entry_hash`).
- `--out <path>` writes the per-entity trace output to a file, creating parent directories if absent. The format follows the flags, never the file name: `--as-json` writes one JSON array for a one-shot capture and JSONL (one object per line) under `--live` or a resumed file capture. A JSON resume refuses an existing one-shot array or incompatible file before changing it; use a new file or a JSONL capture. Without `--as-json` it writes the rendered text table whatever the file is called.
- `--live` streams new trace and ledger entries in real time until the referenced run reaches a terminal state (`run.closed` / terminal task transitions) or is interrupted. Reused task ids remain live while any related run occurrence is active. JSONL stdout contains only data; lifecycle notices go to stderr (#5114, #6397).
