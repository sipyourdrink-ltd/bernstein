## Deep-collection adapter selection is a registry, not a dispatch branch

`agent_discovery` chose its deep collectors from `_RICH_DETECTOR_NAMES`, a
table mapping a registry name to the *name of a module-level function*, which
the dispatch loop then resolved through `globals()`. Adding an entity class
meant editing that table and adding a `_detect_*` function beside nine others —
a code change and a release for something that is data, and a review surface
every other detector runs through.

Selection is now `(matcher, adapter)` pairs. `register_detector` adds one,
`resolve_detector` returns the first that matches, and the dispatch loop asks
the registry rather than branching. The nine built-ins are registered as exact
-name matchers and behave exactly as before.

Two properties are pinned by tests because they are easy to lose in a migration
like this. A built-in's function is resolved on every call, not captured at
registration, so the unit tests that monkeypatch `_detect_claude` still work.
And first match wins, so a pair registered later cannot take over an entity a
built-in already claims; a matcher that raises is treated as no match rather
than aborting the pass, since one bad third-party pair must not fail discovery
for everything else.

Slice 2 of #5081. The probe-record format, jitter and per-probe timeouts, and
the per-run journal entry are the other slices.
