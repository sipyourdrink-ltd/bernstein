# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

## Added

- Volunteer tasks enforce `allowed_paths`, re-run the project's gates, and assemble the signed receipt (#4033). `finish_volunteer_task` takes the patch a run produced plus the project's manifest and returns either a `SignedResultBundle` or a `VolunteerRefusal` — never a bundle marked failed, because a signed bundle is a claim the work is acceptable and one that says otherwise in a boolean field is a misreading away from being treated as a pass. Scope is enforced before any gate subprocess starts, and a glob match is not the whole check: the same filesystem containment barrier the rest of the project uses refuses a path that is not repository-relative (`docs/../src/x.py` is a spelling `docs/**` matches) and a path that resolves out of the worktree through a symlinked component, and only then are the project's globs consulted through the one matcher shared with credential scoping. Matching stays case-sensitive on every platform, so a scope written for `src/` cannot be satisfied by `SRC/` on a case-insensitive filesystem. Gates run as argv under one wall-clock budget shared across them rather than one budget each, stop at the first failure, and take an environment built from the sandbox profile rather than inherited from the donor's shell. A passing run's bundle carries the manifest's own digest as `manifest_sha256` and the profile's digest as its sandbox identifier, continuing the manifest → profile → receipt chain, and its worker identity is derived from the signing key so the bundle cannot name one worker while the signature is by another. Refusals carry stable, append-only reason codes. Part of #3869. Docs: [`docs/reference/volunteer-manifest.md`](../reference/volunteer-manifest.md).
- One answer to "which paths does this diff touch" (`bernstein.core.diff_paths`). The Tier-3 auto-heal cordon and volunteer scope enforcement both fail open on a path an extractor misses, so the extraction moved out of `autofix/tier3.py` into a shared module alongside `path_scope`, and gained the shapes it was missing: a content-preserving rename or copy, a mode change, and a binary file each touch a file while printing no hunk at all, and were previously invisible to the cordon. Quoted non-ASCII paths are decoded rather than handed to a matcher as escape sequences, and an ambiguous `diff --git` header contributes every candidate split — the extractor over-approximates on purpose, since a spurious path costs a readable refusal and a missing one costs an unchallenged write. `bernstein.core.autofix.tier3.extract_paths_from_unified_diff` still resolves for existing callers.
## Security

- The agent task-scope gate no longer depends on how FastAPI happens to store
  included routers (#4023). Both matcher sets are compiled from the live
  route table, and from FastAPI 0.137 `include_router` keeps a wrapper object
  in that table instead of copying the sub-router's routes into it — the walk
  saw objects with no `path`, enumerated nothing, and every matcher came back
  empty. The gate then failed in both directions at once: per-task routes
  registered through a sub-router (`/approvals/{task_id}/approve`, the review
  board's per-task decision route) lost their scope check entirely, while
  `/tasks/batch` and the other collection routes lost their exemption and were
  gated as though `batch` were a task id. The walk now lives in
  `core/routes/route_table.py`, descends through those wrappers, re-applies
  each mount prefix, and recognises a wrapper by shape rather than by
  importing a private class, so a future rename degrades to the older
  behaviour instead of raising on import. `tests/unit/test_route_table.py`
  asserts the real app's matcher sets are non-empty, so a framework change
  cannot empty them again while the rest of the suite still passes. The
  `fastapi<0.137` ceiling that #3979 added as a stopgap is removed.

- Issue text from a project a donor does not control is normalised before it
  can become an agent prompt (#4031). `sanitize_issue_text` in
  `core/volunteer/issue_sanitize.py` returns the title and body as one
  delimited block, closing the three channels through which text a reviewer
  never saw could reach the model. HTML comments are stripped in both
  spellings: closed `<!-- ... -->` across any number of lines, and an
  unterminated `<!--`, which opens a CommonMark HTML block whose end condition
  is never met, so the rendered page hides everything after it while the API's
  raw body carries all of it. Invisible characters are dropped explicitly
  rather than left to normalisation — NFKC removes none of the 170 `Cf` format
  characters, so U+200B, U+FEFF and U+202E RIGHT-TO-LEFT OVERRIDE all survive
  it, and a word a reviewer read as one word would otherwise reach the model as
  two. `Cc`, `Cf` and `Cs` characters are removed with newline and tab
  excepted, `\r\n` and a lone `\r` fold to `\n` first so that dropping a
  carriage return cannot glue two lines into one word, and NFKC then runs last,
  which leaves the block itself NFKC-normalised for anything downstream that
  hashes it. The block's fence is derived from the digest of its own content
  and re-derived until it does not occur there: deterministic, so the same
  title and body produce the same bytes in every process and replay stays
  byte-identical, and unforgeable, so a body containing the fence verbatim
  cannot close it early. `normalize_untrusted_text` exposes the same transform
  without the prompt fence. Nothing here asserts model behaviour, and the
  module imports `hashlib`, `re` and `unicodedata` and nothing else, pinned by
  an exact allowlist so it cannot quietly grow a route to a shell, the
  environment, or the network.

- The last hand-rolled filename join in `core/orchestration/worker.py` now
  goes through the shared containment helper (#4106). `_avoid_shim_line_overflow`
  derived its stdin-overflow prompt file as `prompt_dir / f"{session}.stdin-overflow"`
  with no check at all — the only site left in the file where a value reaching
  the function could name a file outside the intended directory. It is now
  `contained_path(prompt_dir, f"{session}.stdin-overflow", label="session id")`,
  the same helper #3802's sweep has been centralising everywhere else (most
  recently in #4095). This is defence in depth rather than a live hole: `session`
  has already passed `_SESSION_ID_RE` in `main()` before the function is ever
  reached, and the allowlists agree on characters, so nothing arriving through
  the CLI is newly refused. Two behaviours are pinned by tests: a session value
  the CLI would have rejected now raises `PathContainmentError` instead of
  naming a file, and an over-long session that passes the regex but exceeds the
  255-byte component cap now fails as `PathTooLongError` at the check instead
  of reaching `open()` as `OSError(ENAMETOOLONG)` (the same capacity-vs-
  containment distinction #4095 recorded for the pid-file sites).
