## Authentication attempt limits

Dashboard login, SSO login and callbacks, and CLI device authorization endpoints
now share a default limit of 10 attempts per client in 60 seconds, including their
`/api/v1` aliases. Exhausted budgets return HTTP 429 with `Retry-After` and an
exponentially increasing lockout. Failed dashboard credentials also trigger
per-client backoff; a successful login clears only that client's backoff.
Existing seed `rate_limit` buckets can override the attempt count and window.
