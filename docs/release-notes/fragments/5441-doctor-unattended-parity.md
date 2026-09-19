## Doctor unattended parity with spawner environment

`bernstein doctor --unattended` routes all adapter and binary reachability probes through the identical environment construction used by the orchestrator spawner. `run_preflight` now defaults to the unattended variant when standard input is not a TTY, preventing false green interactive checks from failing at task spawn time.

Version posture receipts produced interactively and unattended are byte-identical (modulo timestamps), and probe failures in unattended PATH isolation explicitly name the failing probe (#5441).
