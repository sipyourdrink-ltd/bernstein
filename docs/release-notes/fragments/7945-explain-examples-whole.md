## `explain` shows every example whole at 80 columns

`bernstein explain completions` at 80 columns cut the Bash example to
`--shell bash…`, because the examples sat in a no-wrap table column. Each
example is now printed whole on its own line with its description under it, so
the terminal wraps it and redirected output keeps the full command (#7945).
