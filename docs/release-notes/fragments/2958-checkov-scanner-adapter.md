## Checkov scanner adapter

`get_scanner("checkov")` returns a deterministic infrastructure-as-code
adapter. It runs `checkov --directory . --output sarif --skip-download
--soft-fail` from inside the target and normalises the SARIF into findings
keyed on check id, path and a hash of the flagged resource block.

Checkov's built-in checks ship with the package, so the recorded version pins
the ruleset. `--skip-download` keeps Checkov off Prisma Cloud. The recorded
ruleset digest covers a configured `--config-file` and any `.checkov.yaml` /
`.checkov.yml` in the target. `HOME` points at an empty directory owned by
the adapter, so an operator's `~/.checkov.yaml` cannot change a scan.
