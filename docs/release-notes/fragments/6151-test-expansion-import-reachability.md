## test_expansion checks imports from tests changed in the same task

The optional `test_expansion` gate now reports changed production modules that
have no direct or transitive import from a changed test, replacing filename
matching. Required configurations block on uncovered modules. The check measures
static module imports, not symbol calls or runtime coverage; dynamic imports
remain unresolved. Its process-local graph cache validates all source/test bytes
and prunes vendor/build trees in flat projects. The gate-evasion fixture now
measures the no-module-import placeholder case. Partial implementation of #6151.
