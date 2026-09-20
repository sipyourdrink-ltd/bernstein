## Coverage ratchet workflow no longer checks out an untrusted `workflow_run` SHA

The `coverage-ratchet.yml` workflow now checks out `main` first, then proves the
event's `head_sha` is an ancestor of `main` with `git merge-base --is-ancestor`
before checking it out. The checkout step no longer interpolates
`github.event.workflow_run.*`, so Scorecard Dangerous-Workflow reports 10 (was 0).

The job-level guard (`github.event.workflow_run.head_repository.full_name ==
github.repository && github.event.workflow_run.event == 'push'`) is kept; the
ancestor check adds the missing provenance link between the event SHA and the
default branch's history.
