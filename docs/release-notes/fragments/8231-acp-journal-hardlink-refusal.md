## Refuse hardlinked and non-regular ACP session journals

`resolve_session_journal_path` and `project_journal_to_acp_updates` now verify
ACP session journals with `lstat` and descriptor-level `O_NOFOLLOW` + `fstat`,
refusing hardlinks (`st_nlink != 1`) and non-regular entries (such as FIFOs or
directories named `journal.jsonl`) in addition to symlinks.
