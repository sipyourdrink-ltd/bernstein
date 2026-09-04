## A leading dot in a filename is part of the name

Two path normalisers wrote `lstrip("./")` where they meant "drop a redundant `./` prefix". `str.lstrip` takes a set of characters, not a prefix, so it removed every leading `.` and `/`: `.github/workflows/ci.yml` became `github/workflows/ci.yml`.

In the tag-conformance check (`core/admission/tags.py`) that let a changed path satisfy an allow-prefix from a directory the contract never named, so a task declaring `docs-only` could write `.docs/` and receive a signed receipt saying it was conformant.

In `bernstein history` (`cli/commands/maintenance_cmd.py`) only the relative branch called it, so the two spellings of one path disagreed and the command reported no history for a dotfile named by an absolute path, while also echoing the wrong name back.

Both now normalise through `Path.as_posix`, which already drops the redundant `.` and `//` components and leaves a leading dot alone (#5498).
