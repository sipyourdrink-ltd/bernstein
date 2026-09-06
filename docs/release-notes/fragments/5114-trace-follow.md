## `bernstein trace follow` reports every trace that references one entity

`trace show` globs `.sdd/traces/` for a task id and prints whichever file
matched, once. An entity id — a task, a run, a grant — appears across several
traces, and nothing joined them, so following one across a run meant exporting
the store and grepping it.

`bernstein trace follow <entity-id>` reports every index entry that references
the entity, oldest first, with `--as-json` for a machine reader. The entity is
matched against the trace id, the task id and the digest, which are the three
spellings by which an index row can name one.

Ordering is by start time with trace id as the tiebreak, and timestamps render
in UTC, so a finished run prints byte-identically on every invocation rather
than inheriting the order traces happened to be written in (#5114).
