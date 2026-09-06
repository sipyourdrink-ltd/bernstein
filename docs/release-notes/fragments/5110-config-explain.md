## `bernstein config explain` names the layer every value came from

"Why is this value what it is" had no command. `config list` printed the
effective value and its source but never the file it was read from, and emitted
nothing a script could read, so a CI check that wanted to assert "this came from
the project layer, not from an environment variable" had nothing to gate on.

`bernstein config explain [KEY]` prints every effective value with its layer and
the path it was read from, highest-precedence first. `--json` emits
`{precedence, settings[{key, value, layer, path, chain}]}` — carrying the order
it resolved by, so a caller does not hardcode it. Printed values are the
redacted ones: a resolution report is what an operator pastes into an issue.

The precedence order now has one definition in code, `CONFIG_PRECEDENCE`. It had
been written out in prose twice in `core/config/home.py` and both copies had
gone stale — the module header described four layers and `resolve_config`
described five, while the resolver has built six since `context` was added.
Neither is restated now, and a test fails if a layer is added to `ConfigSource`
without being placed in the order (#5110).
