## `bernstein verify` picks a verifier from the artefact itself

`bernstein verify <path>` now accepts a file positional, not only the
wheelhouse directory it took before -- a file argument was previously a
usage error, so this is additive. Given a file, it reads the artefact and
dispatches to the verifier for its kind: an explicit `"kind"` field wins
when present, and each registered verifier's own sniffing predicate is
tried when it is not. Two kinds are wired so far, `bom` and
`receipt-bundle`, each returning the same result shape and the same exit
code as the artefact's original `<group> verify` command. An artefact
matching no registered kind exits `3`, distinct from a verification
failure (`1`) and a Click usage error (`2`). (#5103)
