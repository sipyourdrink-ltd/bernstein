---
A single infrastructure flake no longer holds every merge in the repository
---

- The trunk-health SLO now requires at least two failed runs in the window before opening a `trunk-unstable` marker. At the sample sizes the 24h window produces, a 5% threshold was already crossed by one failure, so the gate behaved as zero-tolerance while reading as a rate.
- The andon decision moved into `marker_should_open()` so the condition that can hold the whole repository is reachable from a test.
- The shipped-compose test treats a `docker compose` probe that does not answer within its timeout as "CLI unavailable" and skips, instead of letting `TimeoutExpired` fail the module as though a compose file were malformed.
