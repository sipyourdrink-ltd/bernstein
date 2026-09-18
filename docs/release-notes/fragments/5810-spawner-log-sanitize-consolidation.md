## Spawner log sanitisation now escapes the full control-character set

The spawner modules' private `_sanitise_for_log` helper (which stripped only
CR/LF) is replaced by `bernstein.core.security.sanitize.sanitize_log`, which
additionally escapes tab, DEL, C1 controls, and U+2028/U+2029 before they can
reach a log sink. Behaviour at existing log sites is unchanged for benign
input; the `_sanitise_for_log` alias is removed from
`bernstein.core.agents.spawner`'s exports (it was private API).