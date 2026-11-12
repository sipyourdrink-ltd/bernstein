# Declaration initialization quarantine

The default skill loader disables a source whose `iter_skills()` fails, or an
individual artifact it cannot register (including a duplicate name). Other
sources and artifacts continue loading. The first failure records its source,
origin, skill name when known, exception text and class, and Unix timestamp in
`.sdd/runtime/declaration-quarantine.json`. A `declaration.quarantined` event in
the workspace HMAC audit chain carries the same record.

Markers have no threshold or expiry. Reloading or restarting does not retry a
disabled declaration or emit another failure event. An artifact marker blocks
that origin and name, leaving the winning declaration and its siblings usable.
A source marker blocks enumeration of that entire source.

Inspect the workspace with:

```bash
bernstein status --json                    # quarantined_declarations count
bernstein doctor --json                    # Declaration quarantine finding
bernstein govern audit --only doctor --json # doctor:declaration-quarantine
```

Doctor and audit include the declaration ID, reason, origin and failure time.
Fix the cause, then explicitly re-enable the declaration:

```bash
bernstein quarantine re-enable DECLARATION_ID --workdir /path/to/workspace
```

The command journals `declaration.reenabled` with the local operator username
and release timestamp before removing the marker. The next loader construction
or refresh can attempt initialization again. If it still fails, a new marker
and failure event are created. `doctor --fix` never releases these markers.
Unknown IDs fail with exit code 1; a successful release exits 0.

State reads do not initialize declarations or create an audit key. Broken state
is reported as a diagnostic failure rather than treated as an empty quarantine.
The status command still requires the task server, as for its other counts.

This store is separate from task quarantine (`.sdd/runtime/quarantine.json`);
`quarantine list` and `quarantine clear` retain their task-only behavior. Tracker
adapter and plugin-factory import isolation remain follow-up work.

The scheduler, webhook task gate and manual trigger command share the routine
markers. A named routine that fails to load is disabled while valid neighbors
continue. For a directly mapped event with no matching active rule, an enabled
quarantined rule for that source suppresses task creation. An unknown source
or unreadable policy also blocks that fallback. `bernstein triggers list`
shows quarantined entries without recording new failures.
