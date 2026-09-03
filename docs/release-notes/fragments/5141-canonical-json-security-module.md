## New Canonical JSON Security Module (`core.security.canonical`)

Bernstein now exposes a single canonical-JSON byte rule for all hashing and signing operations through the new `bernstein.core.security.canonical` public module.

### Public API

- `CANONICALIZATION_VERSION` / `CANONICALIZATION_FIELD` — versioned identity markers
- `canonical_bytes(payload)` — RFC 8785/JCS canonical bytes with `allow_nan=False` hard guarantee
- `legacy_ascii_bytes(payload)` — ASCII-escaped canonical form for legacy compatibility
- `legacy_pretty_bytes(payload)` — indented ASCII with trailing newline for human-readable artefacts
- `bytes_for_verification(artifact_name, payload)` — verification encoder that raises `UnsupportedCanonicalization` on unknown versions
- `UnsupportedCanonicalization` — raised fail-closed when a build cannot reproduce an artefact's encoding

### Security Properties

- **NaN/Infinity rejection**: Any payload containing NaN, Infinity, or `-Infinity` now raises at the producer rather than producing bytes no strict parser accepts (R9 compliance)
- **Fail-closed verification**: `bytes_for_verification` raises on unknown canonicalization versions rather than returning a mismatch, preventing silent acceptance of unverifiable artefacts
- **Single source of truth**: All byte-identical canonical-JSON sites now route through this module, eliminating drift between independent implementations

This is a security property change: operators depending on the canonical encoding for hashing or signing should verify their payloads do not contain NaN or Infinity values.
