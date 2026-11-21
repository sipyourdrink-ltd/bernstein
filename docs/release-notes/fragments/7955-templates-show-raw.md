## `templates show` prints the template as stored

`bernstein templates show` printed the template through Rich, which dropped
bracketed YAML values such as `depends_on: [models]` as markup and wrapped long
lines. It now writes the stored text unchanged, so redirected output matches
the file `bernstein templates use` writes (#7955).
