## Spawned auggie agents run in print mode and see their credentials

The `auggie` adapter ran `auggie --allow-indexing <prompt>` with no TTY.
auggie 0.36 then printed "Interactive mode requires a terminal", said it was
switching to print mode, and did not exit. An agent that could not even
authenticate held its slot until the watchdog killed it. The adapter now
passes `--print`, and a missing login fails at once with the reason.

The adapter also forwarded `AUGMENT_API_KEY` and `AUGMENT_TOKEN`, which auggie
never reads. It now forwards `AUGMENT_SESSION_AUTH`, the documented headless
credential, and the `AUGMENT_API_TOKEN` / `AUGMENT_API_URL` pair.

auggie gets a contract (`tests/contract/contracts/auggie.yaml`) pinning
`--print` and `--allow-indexing`. Admission refuses an adapter with no
contract, so this is the first step to an admissible auggie.
