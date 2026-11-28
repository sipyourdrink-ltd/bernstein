## `runs report --since` refuses durations that name no past window

`bernstein runs report --since nonsense` exited with a Python traceback, and
`nan`, `inf` and `-2h` were accepted. A malformed, non-finite or negative
duration is now a usage error that names `--since` and shows an example. A
supported duration still filters runs by their start time (#7942).
