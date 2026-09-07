## The adapter capability table stops drifting from the matrix it documents

`docs/adapters/capability_contract.md` names itself an excerpt of
`STRATEGY_MATRIX`, but nothing checked that claim, so three rows had gone
stale against the declaration they were supposed to mirror: `copilot`'s
dangerous mode read `unsupported` where the adapter passes
`--allow-all-tools --no-ask-user` (`cli-flag`), `kilo`'s event channel read
`text-signals` where the adapter speaks `acp`, and `letta_code`'s read
`text-signals` where the adapter spawns with `--output-format stream-json`
and parses each line with `json.loads`. An operator picking an adapter from
this table was reading the wrong capability for all three.

The three rows now match the matrix, and a test parses the table and
cross-checks every adapter it lists against `strategy_table()`, so the next
matrix correction that forgets the doc fails CI instead of sitting unnoticed.
The table remains an excerpt — ten declared adapters are still absent from it,
which is a separate question from whether the rows it does carry are true.
