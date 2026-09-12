## Two fabricated sample outputs in getting-started corrected

`quickstart-tutorial.md` showed `bernstein --version` printing `bernstein 3.5.0`.
The real output is `bernstein, version 3.17.0` — the format was wrong as well as
the number, so a reader comparing their terminal to the page saw a mismatch on
their first command.

`first-run.md` showed `bernstein status` printing a three-line text block
(`Tasks: 0 open · 1 in-progress · …`). No such format string exists anywhere in
the CLI; the command prints a banner, task counts, and a Rich **Bernstein
Agents** table. The page now describes what the command renders and points at
`bernstein status --json` for a parseable shape, rather than quoting output that
was never produced.

Found by running every command invocation in the narrative getting-started
pages against the current CLI rather than reading them (#3619).
