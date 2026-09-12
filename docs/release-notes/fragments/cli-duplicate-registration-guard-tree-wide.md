## Duplicate CLI command registration is now caught anywhere under `src/`

`test_no_command_registered_twice_on_the_same_group` read only `cli/main.py`
and only the `add_command` spelling. Three real collisions slipped past it -
two `@bench_group.command(name="compare")` in `eval/bench/bench_cli.py`, a
second `audit` on `govern_group` in `cli/commands/governance_cmd.py`, and two
`cli.add_command(govern_group, "govern")` that only met after a rebase. Each
is the same mechanism: `Group.add_command` is a dict assignment, so the later
registration silently replaces the earlier one with no error at import or at
run time.

A tree-wide companion now scans every module under `src/bernstein` for both
spellings, resolving each group identifier to the module that owns it so two
files that each define their own `catalog_group` are correctly two groups.
