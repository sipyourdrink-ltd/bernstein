## Every adapter kill now returns a process-reap receipt

The internal manager and prompt-caching wrapper now preserve the adapter kill
contract: every reap attempt returns a structured receipt naming its target.
Manager processes use the same graceful-then-force reap path as other adapters,
and a cached response's virtual PID records a truthful no-op receipt rather than
leaving a gap in the audit trail. (#5015)
