## A module under `core/` with no importer now fails CI

A structural test walks `src/bernstein/core`, computes static import
reachability, and fails the build for any module that is neither reachable nor
named with a reason in `tests/unit/core_reachability_allowlist.txt`. An entry
with no reason, or one whose module has since become reachable, fails too, so
the list can only shrink.

Reachability is computed the way the package resolves imports: the legacy
`_CoreRedirectFinder` alias table is applied before a name is judged, the alias
table itself and compat redirects are excluded from the scan, and a package
`__init__.py` carries reachability only once something outside it imports the
package. The allowlist starts from the tree at the commit the guard turns on;
a module reached by a dynamic loader or an entry point carries a reason naming
that mechanism, and an unreachable module whose wire-or-delete decision is
still open carries the audit it is waiting on (#4645).
