## The CodeQL sanitizer model pack is actually loaded now

`.github/codeql/models` holds a CodeQL model pack declaring this project's log
sanitizers as barriers for the `py/log-injection` query. It was referenced from
`.github/codeql/codeql-config.yml` as a filesystem path, which `database init`
rejects — not with an error, but with

    WARNING: Invalid CodeQL pack specification: './.github/codeql/models'. Ignoring.

so every analysis since the pack was added ran without it. Nothing in the
repository showed the gap: the warning is one line in a job log, the analysis
succeeds, and the alerts it raises look like ordinary findings.

The pack is now handed to `database run-queries` through the analyze step, and
`sanitize_log` and `for_log` are modelled as the barriers they are, so a log
call that already escapes record boundaries and control characters no longer
raises an alert.
