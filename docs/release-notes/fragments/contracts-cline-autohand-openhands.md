## Contracts for the cline, autohand and openhands adapters

Admission refuses an adapter that has no contract. `cline`, `autohand` and
`openhands` now have one in `tests/contract/contracts/`, pinning the flags
each adapter always passes: `cline --yolo`; `autohand --unrestricted
--prompt`; `openhands --headless --override-with-envs --task`. Each was
checked with `bernstein adapters contract-check` against the current release
of its CLI (cline 3.0.72, autohand 0.9.8, OpenHands CLI 1.16.0).
