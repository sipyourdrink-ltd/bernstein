## A permissive-mode test no longer sets the mode for the rest of its worker

The autouse fixture in the MCP input-validation tests restored a prior
`BERNSTEIN_MCP_VALIDATION` value but left the variable set when there had been
no prior value, so a permissive-mode test leaked `permissive` into every later
test on the same pytest-xdist worker. The post-artifact schema tests failed
intermittently as a result. The fixture now restores exactly what it found,
unset or set, and is the only thing that restores the variable; a separate
pytest session run in both directions checks the environment after the file
(#5952).
