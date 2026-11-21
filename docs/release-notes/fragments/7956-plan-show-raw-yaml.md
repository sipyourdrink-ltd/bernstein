## `plan show` prints the stored YAML body

`bernstein plan show` printed the plan through Rich, so a value such as
`'[bold]Garden[/bold]'` lost its brackets and long lines (and the `# path:`
comment) wrapped at the terminal width. The comment lines and the body are now
written as plain text, and the body after the comments matches the stored
file (#7956).
