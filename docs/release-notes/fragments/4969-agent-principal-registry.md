## Project agent principals from the audit chain

`bernstein identity agents` now lists every agent principal that has appeared in a verified grant or delegation record, folded from the tamper-evident audit chain under a chosen install root. Each entry names the principals that issued, were granted, or were delegated to, the grant ids that produced the principal, and the capability ceiling in force at the requested point in time. The subcommand supports `--root`, `--json`, `--as-of`, `--trust-domain`, `--install-key`, and `--verify` for scripted and offline verification.

(#4969)