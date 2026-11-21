## Recovery hints name commands that exist

Two recovery hints told the operator to run a command the CLI rejects.
The unknown-error panel suggested `bernstein run --verbose`; `--verbose`
is a group option, so the hint is now `bernstein --verbose run`. The
air-gap doctor's MCP row suggested `bernstein mcp catalog remove`; the
subcommand is `uninstall`.

The model-unreachable panel said `BERNSTEIN_OFFLINE=1` switches to the
mock adapter. It does not: the variable only makes the doctor skip its
network probes. The panel now points at
`bernstein doctor extended --provider <name>`, which probes the
provider's endpoint.
