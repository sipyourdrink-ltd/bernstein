## Worker reputation counts are computed from merged pull requests

`bernstein.core.volunteer.reputation` holds the earn-only scoring rules for
volunteer workers, and `scripts/generate-earn-only-acceptance-rate.py` turns a
month of merged pull requests into per-worker counts. Attribution reads the
`worker_keyid` and bundle references carried in the pull request body, so a
worker is credited by the identity that signed the work rather than by the
account that opened the pull request. Scores only ever accrue: a reverted pull
request is recorded alongside the merge instead of subtracting from the total,
which keeps a count reproducible from the same month of history no matter when
it is generated (#3879).
