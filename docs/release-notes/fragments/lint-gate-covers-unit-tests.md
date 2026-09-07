## The lint gate covers `tests/unit`

CI ran `ruff check` and `ruff format --check` over `src/` only. `tests/unit`
is where contributors add code every day, and being ungated, it had drifted:
two files with unsorted import blocks and one the formatter would rewrite.
Those three are fixed, and the gate now covers the directory so the next one
fails on the pull request that introduced it.

Scoped to `tests/unit` rather than all of `tests/`. The fixture generators
under `tests/fixtures` carry deliberate formatting the formatter would
flatten — one writes `"\U0001D11E"` to match the codepoint named in the
comment directly above it, and `ruff format` lowercases the hex digits.
Gating those would trade a real explanation for a mechanical rule.
