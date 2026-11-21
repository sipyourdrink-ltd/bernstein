## `prompts compare --json` writes parseable JSON

`bernstein prompts compare NAME V1 V2 --json` printed the JSON through Rich,
which wrapped long strings at a narrow terminal width and dropped `[bold]` from
a prompt name as markup, so stdout did not parse. The command now writes the
JSON unchanged (#7871).
