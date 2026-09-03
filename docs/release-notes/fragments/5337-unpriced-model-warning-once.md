## Unpriced model names warn once and read `unpriced`, not `$0.00`

A model name with no pricing-table entry (a gateway alias, for example) logged the full
`no pricing-table entry` warning on every priced call, and the cost-estimate lines quoted
`$0.00` as if the run were free. The warning now fires once per distinct name per process,
later calls meter at `$0` silently with `priced=False` so totals still carry the tokens, and
the dry-run, preflight and bootstrap estimate lines say `unpriced` for such a name. A real
`:free` route is unchanged (#5337).
