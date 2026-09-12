## Delegation's file-scope axis is gradable, not just recorded

`DelegationScope`'s `allowed_files` axis is a glob field, and a glob is not
a path prefix -- `src/**` covers `src/core`, but `src` as a pattern admits
only the literal path `src`. Reusing the path-prefix ancestry primitive for
this axis would report narrowing that never happened, so it was previously
recorded verbatim on every delegation receipt and graded
`comparison_axis_unsupported`: a hop could prove its `task_ids` narrowing
held and had nothing to say about the axis an operator most often asks
about. Worse, a child that dropped its parent's file restriction entirely
went completely uncompared -- a genuine widening that read as a clean pass.

`bernstein.core.path_scope.pattern_subsumes` decides glob containment
directly over the pattern grammar (segments, `*`, `?`, `**`), and
`bernstein.core.security.capability_tokens.glob_narrows` wraps it with the
same `None`-is-widest convention every other narrowing primitive uses. The
file axis on `DelegationScope` now grades `pass` when a hop's file scope
narrows correctly and `axis_widened` when it does not, the same as every
other axis (#5418).
