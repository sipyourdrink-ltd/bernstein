## Checkov scanner adapter

`get_scanner("checkov")` returns a deterministic infrastructure-as-code
adapter. It runs `checkov --directory . --output sarif --skip-download
--soft-fail --download-external-modules false` from inside the target.
Finding identity includes check id, normalized path, severity, summary and
the flagged resource snippet hash; cosmetic line shifts do not change it.

Checkov's built-in checks ship with the package, so the recorded version pins
the ruleset. `--skip-download` keeps Checkov off Prisma Cloud. The recorded
ruleset digest covers a configured `--config-file` and any `.checkov.yaml` /
`.checkov.yml` in the target. Configurations declaring `external-checks-dir`,
`external-checks-git` or `download-external-modules` are refused before any
subprocess, for both target and caller-supplied files. `HOME` and Windows
`USERPROFILE` point at a fresh temporary directory for each scan, preventing
operator or prior-scan home configuration from changing the ruleset. Results
retain the recorded invocation digest. Partial implementation of #2958.
