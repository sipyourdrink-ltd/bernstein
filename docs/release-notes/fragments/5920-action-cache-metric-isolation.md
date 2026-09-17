## The action-cache metric tests no longer read a process-global counter

`TestMetrics` in `test_action_cache.py` measured the Prometheus hit and savings
counters on the default registry, which is process-global. Under a wider test
selection the two assertions failed while the cache itself was correct, so
whether they passed depended on what else ran in the same worker -- and a real
regression in these counters could have been masked or manufactured by an
unrelated test. Each test now gets its own registry and its own counter objects
(#5920).
