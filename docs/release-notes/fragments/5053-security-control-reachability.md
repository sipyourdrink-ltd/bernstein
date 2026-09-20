## A security or identity control with no caller now fails CI

Several security- and identity-critical modules are implemented, unit-tested
and merged while never invoked from a live code path. Their only importers
are the module's own `__init__` re-export, the lazy module map in
`core/__init__.py`, or a test, so the code looks like coverage and greps as
live without ever running.

A new additive gate scans `core/security/` and `core/identity/` for public
symbols with no caller in the shipped package and fails on an unlisted one.
Known-unreached symbols carry one written reason each in
`unreachable_controls_allowlist.txt`; an entry with no reason, or one whose
symbol became reachable, fails the gate too. The gate runs in its own
workflow, `security-control-reachability.yml`, and never modifies the
protected `ci.yml` (#5053).
