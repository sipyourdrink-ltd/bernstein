# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

## MCP and skill catalogs

MCP catalog fetch is now conditional (ETag/If-None-Match) and shares one cache across workers.

- Inside the revalidation window a fetch makes no request, and after it a `304` reuses the cached copy.
- `bernstein mcp serve` startup no longer forces a refresh, so starting many agents does not multiply requests.
- A response that fails schema validation is remembered by its `ETag` and is not downloaded again until it changes. The last valid catalog stays cached.
- The skill catalog uses the same fetch path. Its cache moves from the per-project `.sdd/skills_catalog/` to one user-level file under `$XDG_CACHE_HOME/bernstein/`, overridable with `BERNSTEIN_SKILLS_CATALOG_CACHE_PATH`. Revocations cached in the old location keep applying until the new cache is populated.
- Cache writes use a unique temp file and an atomic rename, so parallel workers cannot clobber each other. A failed cache write is logged and no longer fails the fetch.
