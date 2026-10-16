## Bandit scanner adapter

`get_scanner("bandit")` returns a deterministic SAST adapter for Python
sources. It runs `bandit --recursive --exit-zero --format sarif` and
normalises the SARIF into findings keyed on rule, path and a hash of the
flagged snippet, so a line shift elsewhere in a file does not change a
finding's identity.

Bandit's checks ship with the package, so the recorded version pins the
ruleset. The recorded ruleset digest also covers a `-c` config file and any
`.bandit` file that Bandit finds in the target tree on its own. The SARIF
formatter is an optional extra: a Bandit without `bandit[sarif]` is refused
with that remedy.
