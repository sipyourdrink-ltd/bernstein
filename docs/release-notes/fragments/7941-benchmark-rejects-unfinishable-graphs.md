## The offline benchmark refuses task graphs that cannot finish

`benchmarks/run_benchmark.py` accepted a subtask that depends on an unknown
subtask, and a dependency cycle. The scheduler stopped with steps left, and the
run exited 0 and wrote partial times as results. The loader now refuses both,
naming the task and the problem, and the run exits 1 before it writes any
result file. A forward reference stays valid (#7941).
