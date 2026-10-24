## Bandit scanner adapter

`get_scanner("bandit")` returns a deterministic SAST adapter for Python
sources. It runs `bandit --recursive --quiet --exit-zero --format sarif` and
normalises the SARIF into findings hashed from rule, portable path, severity,
message, snippet hash and confidence. Source coordinates are excluded, so a
line shift elsewhere in a file does not change a finding's identity.

Bandit's checks ship with the package, so the recorded version pins the
ruleset. The recorded ruleset digest also covers a `-c` config file and any
`.bandit` file that Bandit finds in the target tree on its own. The SARIF
formatter is an optional extra: a Bandit without `bandit[sarif]` is refused
with that remedy.

File URIs retain portable paths on Windows drives and UNC shares. Scan results
carry their invocation digest even when there are no findings, and a SARIF
report declaring unsuccessful execution is rejected. Partial implementation
of #2958. (#2958)
