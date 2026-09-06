## One broken skill source no longer takes every other skill with it

`SkillLoader` re-scanned every source with no exception handling, and its
constructor calls that scan directly — so a source whose `iter_skills()`
threw raised out of `SkillLoader(...)` and the caller got no loader at
all. A single duplicate skill name did the same through
`DuplicateSkillError`.

Failures are now isolated per source and per artifact, and recorded as
`QuarantinedSkill`: which source, which skill (or `None` when a source
threw before naming one), the exception text, its type, and when. The
rest of the set loads.

A name conflict is still never silent — the first origin still wins, so
precedence follows source order exactly as before — it is reported
instead of fatal. `SkillLoader(..., on_quarantine=...)` is called once per
entry as it happens, for a caller that wants to journal a governance
event; a hook that raises is itself caught (#5108).
