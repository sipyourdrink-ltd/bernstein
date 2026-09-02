# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.


## Lineage

- Lineage entries carry an additive optional `sensitivity` classification, and the effective sensitivity of an artefact is the maximum class over its lineage closure — so a summary of a confidential document is confidential. Absence fails closed to the highest class, and the verdict names the closure member that raised the level and the path through the graph that reaches it. An entry without the field canonicalises byte-identically to the pre-change schema, so every historical signature and HMAC is untouched (#5042).

## Adapters

- The Codex adapter chooses its sandbox argv from its declared dangerous-mode strategy instead of hardcoding `--sandbox workspace-write`. That profile is implemented with bubblewrap and cannot initialise in a containerised runner that drops capabilities or denies unprivileged user namespaces; every model-issued shell command then fails while `codex exec` still exits 0 with an empty diff, so the run reads as success. An operator whose runner already isolates the process declares `ALWAYS_ON` and the spawn passes `--dangerously-bypass-approvals-and-sandbox`; the default is unchanged and keeps the vendor sandbox. The Claude-tier fallback model moves from `gpt-5.4`, which upstream now rejects with HTTP 400 on the ChatGPT-account auth path, to `gpt-5.5`, and the module's verified-against version is refreshed to @openai/codex 0.152.1.
