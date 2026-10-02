## A test that reconfigures root logging no longer empties a later test's `caplog`

`bernstein --quiet` runs `logging.basicConfig(level=logging.ERROR,
force=True)`, which is right for the program and was never undone when a test
ran the CLI in-process. Every `bernstein.*` logger inherits the root level, so
a later test on the same worker that asserted on a WARNING read an empty
`caplog` against correct code. An autouse fixture in `tests/conftest.py` now
restores the root level, `logging.disable`, and the non-pytest root handlers
after every test (#6296).
