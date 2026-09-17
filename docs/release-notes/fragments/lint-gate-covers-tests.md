## The lint gate covers `tests/`

CI ran `ruff check` and `ruff format --check` over `src/` only, so `tests/`
— where contributors add code every day — was ungated on the pull request
and drifted between runs. The nightly drift sweep (`nightly-drift-sweep.yml`)
formats the whole tree, so the drift was healed after the fact rather than
caught by the change that introduced it.

`tests/` passes both checks today, so the gate now covers it and the next
drift fails on the pull request that introduced it.
