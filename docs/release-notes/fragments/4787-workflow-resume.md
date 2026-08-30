## bernstein workflow resume command

The ``bernstein workflow resume <run_id>`` command resumes a paused or killed
workflow run from the last completed node. Spec digest validation prevents
resuming with a modified manifest. The command re-resolves the manifest from
the path stored at run start, or from an explicit ``--manifest`` argument.
