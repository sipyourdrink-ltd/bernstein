## Workflow commands report a manifest that is not valid UTF-8

A workflow file with an invalid UTF-8 byte made `bernstein workflow validate`
and `bernstein workflow run --dry-run` print a Python traceback, and
`bernstein workflow list --dir` stopped at that file. The loader now raises a
`WorkflowSpecError` that names the file and the byte, so validation and dry run
exit 1 with that message and the listing shows the file on its own error row
next to the valid manifests (#7869).
