## Orphan security modules must carry a machine-checkable reason

The orphan-module guard for `core/security` now rejects free-text reasons and dates that do not parse. An entry needs an issue reference (`#NNNN`) or a valid, unexpired `remove-by:YYYY-MM-DD` (#5505).
