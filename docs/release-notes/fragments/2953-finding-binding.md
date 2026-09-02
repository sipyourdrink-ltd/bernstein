### Fixed

- The `finding` artifact canonicalisation rule now rejects payloads that were previously silently accepted. Missing or blank required fields, non-string values, and unknown keys now raise `CanonicalisationError` instead of producing a stable identity hash. The `artifact_content_hash(ArtifactKind.FINDING, ...)` contract is now enforced. ([#2953](https://github.com/sipyourdrink-ltd/bernstein/issues/2953))
