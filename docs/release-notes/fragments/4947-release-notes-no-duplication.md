## Release notes fragments are now consumed during rotation

Fixed `rotate_into()` in `scripts/rotate_release_notes.py` to delete fragment files without appending their content verbatim to the version page. The version page now contains only curated notes, not raw fragments. This ensures release notes reflect intentional editing, not mechanical aggregation. (#4947)
