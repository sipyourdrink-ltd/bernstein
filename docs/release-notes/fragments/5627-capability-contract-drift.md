## Adapter capability contract table synced with STRATEGY_MATRIX

The capability matrix table in `docs/adapters/capability_contract.md` drifted from
authoritative declarations in `STRATEGY_MATRIX` for `copilot` (`dangerous_mode` is
`cli-flag`), `kilo` (`event_channel` is `acp`), and `letta_code` (`event_channel`
is `stream-json`). The rows have been aligned and protected by a regression test.

(#5627)
