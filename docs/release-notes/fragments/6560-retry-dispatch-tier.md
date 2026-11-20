### Fixed

- Preserve backend dispatch tiers through both task retry paths. Reopen can explicitly select a validated tier, recording the previous tier, selected tier and reason with the persisted transition; omission preserves the current tier. Automatic failure-based retiering remains outside this change. Refs #6560.
