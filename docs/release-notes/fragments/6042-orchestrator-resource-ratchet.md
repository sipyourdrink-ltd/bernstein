## Pin the orchestrator unit file's process budget (#6042)

`tests/unit/test_orchestrator.py` now fails the test whose teardown leaves
its pytest subprocess over a thread/RSS budget. The intermittent
`FAIL tests/unit/test_orchestrator.py (~53s)` with no pytest summary was an
external SIGKILL of the file's subprocess near end-of-run on a
memory-pressured shard runner; unbounded per-test growth had made it the
largest process. The per-test ratchet turns any recurrence into a located,
named failure instead of a silent shard kill, and a probe test pins it.
