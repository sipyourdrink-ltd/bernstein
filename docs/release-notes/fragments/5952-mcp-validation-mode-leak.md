## A permissive-mode test no longer sets the mode for the rest of its worker

The autouse fixture in the MCP input-validation tests restored a prior
`BERNSTEIN_MCP_VALIDATION` value but left the variable set when there had been
no prior value, so a permissive-mode test leaked `permissive` into every later
test on the same pytest-xdist worker. The post-artifact schema tests failed
intermittently as a result. The fixture now restores an unset variable to
unset, and the tests that need a mode go through `monkeypatch` (#5952).
