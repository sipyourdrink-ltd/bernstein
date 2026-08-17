# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

## Security

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
