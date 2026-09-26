## `--verbose`/`--quiet` no longer reconfigure the process-wide root logger

`--quiet` and `--verbose` called `logging.basicConfig(..., force=True)`,
which reconfigures the **root** logger for the rest of the process: every
other logger, including third-party libraries, inherits its level from
root by default. In a long-running process this silently lowered logging
everywhere after one `--quiet` command ran; in a pytest worker that
invokes the CLI (directly or through `CliRunner`), it left every later
test in that worker with a raised root level, so a `caplog` assertion on
an unrelated logger read as "nothing was logged" instead of "the level
was raised" - a false negative that lets a real regression ship green.

Both flags now scope their reconfiguration to the `bernstein` logger
specifically, leaving root and every non-`bernstein` logger untouched.
`tests/conftest.py` also gained an autouse fixture that restores the
`bernstein` logger's level, handlers and propagation after every test, so
even a test that runs a `--quiet`/`--verbose` command directly cannot leak
into whatever test runs next in the same worker (#6184).
