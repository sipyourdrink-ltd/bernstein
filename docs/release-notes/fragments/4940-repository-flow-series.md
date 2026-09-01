## Repository-flow series persistence

Added an append-only JSONL series for repository-flow samples at
`bernstein.core.observability.repository_flow_series` (#4940).
`append_sample` and `read_samples` share byte-deterministic
serialisation, so two operators reading the same series see
identical records. Malformed lines fail loud with the offending line
number rather than being silently skipped (#4940).
