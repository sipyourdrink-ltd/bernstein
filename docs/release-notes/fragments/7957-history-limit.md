## `history --limit` refuses negative values

`bernstein history` passed `max(1, limit)` to its lookup, so `--limit 0` and
`--limit -1` both returned one archived task. A negative limit is now a usage
error that names `--limit`, `0` returns no records in table and JSON output,
and positive limits are unchanged (#7957).
