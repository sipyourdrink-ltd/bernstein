## Bounded waits measure elapsed time with a monotonic clock

Six loops that bound themselves with `time.time()` now read `time.monotonic()`, so a wall-clock step can no longer extend a drain past its limit or end a wait before the work it was waiting on finishes. The sites are orchestrator drain before cleanup, the autofix daemon shutdown, device-code token polling, the Skyvern run poll, and the advanced/recipe run-completion waits (#5721).
