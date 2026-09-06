## A probe is a declared record

Which attribute a discovery pass collects, how it collects it, how long it may
take and what it is allowed to assert were all implicit in code.
`agent_discovery._RICH_DETECTOR_NAMES` named functions, and nothing anywhere
carried a refresh interval, a timeout, a cost class or a taint tag — so adding
one attribute to what a probe reports was a code change and a release, and two
operators could not compare two runs without diffing the source that produced
them.

`bernstein.core.govern.probe` makes a probe data: a stable id, the attribute it
produces, its collection method, refresh interval, hard timeout, cost class,
and the taint tags it may assert. `load_probe_set` reads a directory an
operator can extend, in sorted name order so the set is identical on every
machine, and refuses two declarations sharing an id — the id is what a run
records, and two of them make "which probe produced this" unanswerable.

Unknown fields round-trip unchanged. A probe file written by a newer build
carries fields this one does not interpret; dropping them on load would
silently rewrite the operator's file the next time anything saved it. They are
preserved verbatim and re-emitted, and the round trip is stable across repeated
load-save passes so a file cannot drift on its own.

A probe with no timeout is refused rather than defaulted: a probe with no
ceiling is one that can hang a whole pass.

Slice 1 of #5081. Nothing runs these yet — the collector is #5082.
