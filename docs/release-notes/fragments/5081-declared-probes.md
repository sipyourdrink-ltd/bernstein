## Declared governance probes now have bounded execution

Declared probe sets can cache fresh facts, try ordered fallback sources with bounded retries, time out without blocking the rest of discovery, return explicit unknown values when sources are exhausted, and record one canonical run journal entry. Deep agent-detector registrations now use the same non-blocking deadline boundary so one hung detector cannot stall the full discovery pass (#5081).
