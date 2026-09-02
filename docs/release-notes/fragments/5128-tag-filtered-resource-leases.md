## Tag-filtered resource leases

A new atomic claim primitive (`bernstein.core.sandbox.resource_lease`) lets a process take a lease on a named resource or tag-filtered candidate, with TTL, owner, keepalive, and release on context exit or interpreter exit. Distinct error types separate "no matching resource" from "all matches held". ([#5128](https://github.com/sipyourdrink-ltd/bernstein/issues/5128))
