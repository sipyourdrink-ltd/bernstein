## `retro --archive` refuses a path that is not an archive file

`bernstein retro --archive missing.jsonl` and `--archive <directory>` exited 0
with "No tasks found in the archive." and wrote no report, the same answer as
an empty archive. A path passed to `--archive` that is missing or a directory
is now a usage error that names it. An empty archive, and no archive at the
default path, keep their empty result (#7959).
