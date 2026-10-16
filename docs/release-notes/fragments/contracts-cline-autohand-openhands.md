## Contracts for the cline, autohand, openhands, cody and composio adapters

Admission refuses an adapter that has no contract. `cline`, `autohand`,
`openhands`, `cody` and `composio` now have one in `tests/contract/contracts/`, pinning the flags
each adapter always passes: `cline --yolo`; `autohand --unrestricted
--prompt`; `openhands --headless --override-with-envs --task`; `cody chat --message`;
`ao spawn --prompt`. Each was
checked with `bernstein adapters contract-check` against the current release
of its CLI (cline 3.0.72, autohand 0.9.8, OpenHands CLI 1.16.0, cody 5.5.26,
`@aoagents/ao`).
