## OpenCode's declared event channel now matches what it emits

The OpenCode adapter's capability declaration named its event channel
`text-signals`, even though `opencode.py` has passed `--format json` on
every spawn since #4099 and the CLI emits NDJSON lifecycle events under
that flag. `event_channel` names a property of what the *upstream CLI*
emits (per the axis's own definition in
`docs/adapters/capability_contract.md` and the `EventChannel` docstring in
`_contract.py`) — the same reading the `cursor` adapter is already declared
under despite having no dedicated stream parser either. OpenCode now
declares `stream-json` to match. Nothing changes in what Bernstein actually
parses today: NDJSON consumption for this adapter remains separate,
unbuilt follow-up work (#3676).
