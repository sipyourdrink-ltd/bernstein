## `bernstein context manifest` content-addresses a task's declared context

A new `bernstein context manifest <task-id>` subcommand (plus `--workdir` and
`--json`) derives the **context manifest** for a task: the ordered, deduplicated
set of files the task declares as its own, each addressed by the SHA-256 of its
bytes, with a manifest digest over the whole list. The digest is a function of
the declared path set and the bytes behind it — deriving twice over an unchanged
tree is byte-identical, and a single changed byte moves the digest and names the
entry that moved. A declared path the deriver cannot resolve keeps its position
and records `unmanifested` with a reason code (`missing`, `not_a_file`,
`unreadable`, `outside_root`, `invalid_path`) rather than silently disappearing;
a path that escapes the repository root is refused before its bytes are read
(#3366).
