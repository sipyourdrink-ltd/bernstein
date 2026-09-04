## Remediation plan collection

The `governance plan` command now accepts `--remediation-plan <file>` to collect the remedies a playbook declares for its findings into one unsigned proposal. A finding whose clause declares no remedy is listed as unremediated rather than dropped. (#5079)