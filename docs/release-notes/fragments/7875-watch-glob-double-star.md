## `watch --glob` lets `**/` match zero directories

`bernstein watch --glob "src/**/*.py"`, the example in the watch docs, missed
edits to `src/root.py` while it matched `src/nested/module.py`, because
`fnmatch` needs at least one directory for `**/`. Each `**/` segment now matches
zero or more directories. Patterns without `**/` are unchanged (#7875).
