## `bernstein verify pins` checks the loaded plugin/skill set against a pinned manifest

An install-wide pin manifest now names every plugin and skill the install may
load, each at an exact version and content address, together with the sources
each environment may load them from. A floating specifier — `latest`, `*`,
`^1.2.0`, `1.2` — fails the parse instead of surviving as a warning, so an
install cannot drift onto whatever `latest` resolves to on a given day.

`bernstein verify pins --manifest <file> --loaded <file> [--environment NAME]`
prints one line per divergence in presence, version, content hash, or source
and exits 2 on any drift. Applying the manifest rewrites its state file only
when it differs and appends a decision record carrying the manifest hash
before and after on every apply, no-ops included (#5089).
