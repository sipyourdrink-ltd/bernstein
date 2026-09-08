## A roadmap wave that emits nothing now says why

From a fresh workspace, `.bernstein/scenarios` was documented and unreachable.
`emit_roadmap_wave` checked for `.sdd/roadmaps/open` and returned an empty
list when it was absent — one line before the line that loads the scenario
library. A fresh workspace has no roadmap directory, so an operator who
followed the documentation and wrote scenarios got exactly what an operator
who wrote none got: an empty list and no error.

The library is now loaded before the roadmap check, and every exit carries a
reason from a closed set: `backlog-missing`, `ticket-ceiling`, `no-scenarios`,
`no-roadmap`, `no-eligible-scenarios`, `emitted`. `no-roadmap` reports how
many scenarios were found and skipped, so the case that used to be invisible
is the one that now states itself most plainly. The orchestrator logs it at
warning level when scenarios exist and produced nothing, and at debug when
there was nothing to report.

`emit_roadmap_wave` still returns a plain list of paths, so existing callers
are unchanged; `emit_roadmap_wave_outcome` is the one that answers "why".
