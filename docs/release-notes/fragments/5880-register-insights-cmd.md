## `bernstein insights` is reachable, and `--format json` no longer crashes

`bernstein insights` was fully implemented and documented in
`src/bernstein/cli/AGENTS.md`, but no `add_command` call ever registered it
with the top-level CLI group, so invoking it produced "No such command".
Its `--format json` branch was also broken: it called
`console.print_json` with a raw `dict` positionally, which raises
`TypeError` because that parameter expects a JSON string. Both are fixed:
the command is registered in `src/bernstein/cli/main.py`, and the object is
now passed via `print_json`'s `data=` keyword (#5880).
