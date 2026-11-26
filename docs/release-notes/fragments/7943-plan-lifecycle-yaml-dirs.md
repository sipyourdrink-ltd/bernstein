## Plan queries ignore directories named `*.yaml`

`bernstein plan ls` listed a directory such as `plans/active/notes.yaml/` as a
plan, and `bernstein plan show notes` then raised `IsADirectoryError`. The plan
lifecycle's list and look-up now take regular files only (#7943).
