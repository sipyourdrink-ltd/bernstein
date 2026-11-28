## Chaos history limit is validated

`bernstein chaos status --limit` now rejects negative values with a usage error.
`--limit 0` prints an empty history instead of every recorded event, and positive
values still show the newest events. The operation docs state the same rule.
