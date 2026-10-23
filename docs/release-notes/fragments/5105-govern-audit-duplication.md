## `govern audit --only duplication` reports duplicate implementations in the install

The duplication collector from #5105 now has an operator-facing command. Each
of its checks runs as a `duplication:<name>` check under
`bernstein govern audit --only duplication`. A count over the expected count
fails with the offending `path:line` locations. A check whose subject does not
exist yet is `not_measurable`, never a pass.

The checks describe the installed package, not the audited workspace, so they
are opt-in. A default `govern audit` does not run them, and a clean workspace
stays green. `--list` still shows them. The check registry gains this opt-in
mechanism for any check with `opt_in = True`.
