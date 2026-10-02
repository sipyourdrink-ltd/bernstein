## The action-cache metric tests no longer read a stub another test left behind

`TestMetrics` in `test_action_cache.py` failed under a wider test selection
while the cache itself was correct. The starting value was not the cause: the
tests already asserted on a delta. `_emit_hit_metric` looks both counters up by
name on the prometheus module at call time, and
`test_prometheus_import_fallback.py` re-imports that module under a simulated
import timeout, where every metric is an inert stub. After it, the cache
incremented a stub and the assertion read a stub, so the delta was zero. Each
test now patches its own real counters onto the module, which answers both the
shared registry and the stub, and a miss is asserted to increment nothing
(#5920).
