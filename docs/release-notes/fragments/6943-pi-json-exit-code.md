## Pi failures are no longer reported as success

In `--mode json` pi exits 0 even when its final turn failed; only its text
mode turns a failed turn into exit 1. The Pi adapter now runs pi under
`bernstein.adapters.pi_json_launcher`, which streams the events to the
runner log unchanged and exits 1 when the last assistant message stopped on
`error`, `aborted` or `length`, or automatic retries were exhausted. The
task prompt now follows `--`, so a prompt starting with `-` is not read as a
flag.
