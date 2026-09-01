## `bernstein governance plan` (Issue #4980)

A declared posture and an enumerated environment are two descriptions of the same
thing. `bernstein governance plan` diffs them and emits an ordered change set: what
exists but is not permitted, what is required but absent, and what is permitted with
a wider ceiling than declared. Each entry names the inventory observation that
established the current state and the playbook clause that judges it, so a reviewer
can check a finding without rerunning the inventory.

The plan is anchored in the lineage spine, and every field is a pure function of the
playbook and inventory it was computed from, so the same inputs reach the same plan
and a reviewer can tell a re-run from a changed environment.

A surface the inventory could not read is reported as `UNKNOWN`, never as compliant.
This is the distinction the command exists to preserve: a forbidden or permitted
surface that was never successfully read produces an explicit entry rather than
silence, and a required surface that could not be read is separated from one that is
genuinely absent. An inventory declares an unread surface either per-record or as a
top-level list, for the case where enumeration failed and produced no record at all.

A conformant environment produces an empty plan rather than no output, so "nothing to
change" and "the command did not run" are different observable results.
