## A credential's file scope can be narrowed to a sub-glob

`allowed_files` on an agent credential is a glob set, and attenuating one asks
whether every path the child admits is admitted by the parent. Nothing could
answer that, so the mint refused any child pattern the parent had not declared
verbatim: a parent holding `src/**` could hand out `src/a.py` but not
`src/core/**`. A glob scope could be dropped or carried forward unchanged, and
never actually narrowed.

`pattern_subsumes` in `bernstein.core.path_scope` decides containment between
two patterns against that module's own language rather than `fnmatch`, so `*`
stops at a separator and a pattern is still not a prefix — `src/**` subsumes
`src/core/**`, while `src` subsumes neither `src/core` nor anything else
beneath it. `globs_narrow` lifts it to whole sets beside the other narrowing
primitives in `capability_tokens`.

A child pattern that no single parent pattern subsumes is still refused, so a
scope admitted only by two parent patterns together does not widen a
credential on a technicality (#5418).
