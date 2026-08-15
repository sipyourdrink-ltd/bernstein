# README translation gate (readme-l10n)

Every translated `README.<ietf-tag>.md` mirrors the English
`README.md` section for section. Each translated section carries an
HTML-comment binding to a content hash of the English section it
mirrors. `bernstein readme-l10n verify` recomputes those hashes and
fails on drift, naming the language and the exact stale section
heading. A PR that edits an English section and leaves a translation
behind turns red in the same CI run.

This is the same drift-gate shape as `agents-md verify`: the gate is
the deliverable, the translations are its payload.

## The binding format

Directly under each translated `###` heading sits one HTML comment
line:

```html
<!-- l10n: en="install in 30 seconds" hash="sha256:3f9a1c2b..." -->
```

- `en` is the *English* section heading (the `###` text, no prefix).
- `hash` is a `sha256:` prefix (12 hex chars) of the normalised
  content of the English section it mirrors.
- HTML comments do not render on GitHub, so the binding is machine
  readable and diffable without being visible noise.

The header block (logo, badges, the language links line) and the
footer block (license line, author block) are shared **verbatim**
across translations - they carry no binding; `verify` compares them
for equality instead.

## Adding a language

Adding a language is a data change, not a code change:

1. Append the IETF language tag to `languages` in
   `[tool.bernstein.readme-l10n]` in `pyproject.toml`
   (`zh-Hans`, not `zh-CN` - script subtags determine legibility).
2. Create `README.<tag>.md` mirroring the English section structure:
   - header and footer copied verbatim from `README.md`;
   - each `###` section: translated heading, then a binding comment
     line, then the translated prose;
   - code blocks, command names, flag names, file paths and
     `bernstein` subcommands **never** translated - copy them
     verbatim from the English section.
3. Add one language links line to the English `README.md` header,
   plain text links separated by `·`, no flag emoji.
4. Run `uv run bernstein readme-l10n sync` to (re)compute every
   binding hash, then `uv run bernstein readme-l10n verify --workdir .`
   to confirm.
5. Add yourself to `[tool.bernstein.readme-l10n.owners]`. A translation
   is added by whoever can keep it current, not by whoever happens to
   be merging that week:

   ```toml
   [tool.bernstein.readme-l10n.owners]
   "zh-Hans" = "@your-handle"
   ```

A binding line for a section that does not exist in `README.md`
makes `sync` warn; a translated section without a binding line makes
`verify` fail with the section name.

## Exit codes

CI has to tell "nothing to verify" apart from "the configuration
stopped parsing". Those are different situations and only one of them
is fine, so they get different exit codes.

| Exit | Meaning | Printed |
|---|---|---|
| 0 | every configured language is in sync | `OK` |
| 0 | no `pyproject.toml`, or no configured `languages` | `SKIP` |
| 1 | drift: stale binding, translated code block, paragraph-count mismatch, or a modified verbatim block | `DRIFT` |
| 2 | `pyproject.toml` exists but cannot be read or parsed; `languages` malformed; `owners` table or a handle malformed (`verify`) | `CONFIG` |

Both `verify` and `sync` exit 2 on an unreadable or malformed
`pyproject.toml`. Only `verify` reads the `owners` table, so only
`verify` can exit 2 because of it.

A CI step written as `verify || exit 1` collapses 1 and 2 into one
signal. That is fine for blocking a merge and wrong for diagnosing one:
a stray tab in the TOML would otherwise read as "no languages
configured", and the translations would rot behind a green check.

## What CI checks

`.github/workflows/ci.yml` runs `bernstein readme-l10n verify` in the
Repo hygiene job, adjacent to `agents-md verify`. For every configured
language it checks:

1. **Binding drift** - every prose section binds the current hash of
   its English source. A stale binding names the language and the
   exact section heading; the fix is
   `uv run bernstein readme-l10n sync`.
2. **Code-block fidelity** - fenced code blocks in a translated
   section must be byte-identical (normalised) to the English section
   they mirror. A translated command, flag, path or subcommand is a
   failure, not a warning.
3. **Binding placement** - each binding sits directly under the
   translated heading it mirrors. The same English section bound under
   two headings, or a binding under no heading at all, is a failure:
   either shape lets a section resolve to the wrong span.
4. **Paragraph parity** - a translated section carries the same number
   of paragraph-level blocks as the English section it mirrors.
5. **Verbatim header/footer** - logo, badges, the language links
   line and the license/footer block must equal the English ones.

## Paragraph parity: translations preserve block structure

Hash bindings pin English *content*, and `sync` re-pins them after an
English edit without proving the translation followed. A paragraph
added to the English source could therefore go missing from every
translation while the gate stayed green. Parity closes that hole by
comparing block counts per section.

The contract this places on translators is explicit: **a translation
must preserve the blank-line block structure of the English section it
mirrors.** Blocks are runs of non-blank lines separated by blank lines;
a fenced code block counts as one block; binding comments count as
none.

This is stricter than the hash contract below, deliberately. Hash
normalisation absorbs blank-line reflow of the *English source* so a
formatting pass does not invalidate every binding. Parity compares
English against translation, where a merged pair of paragraphs and a
dropped paragraph are indistinguishable from the outside. Joining two
translated paragraphs that are separate in English is therefore an
exit-1 failure, even though no content is missing. Split them back
apart to match the English layout.

## Hash stability

The hash input is normalised before hashing: trailing whitespace is
stripped per line, blank lines are dropped, lines are joined with
`\n`. This absorbs prettier/editor noise (trailing whitespace,
blank-line reflow) so the gate does not cry wolf on every formatting
pass, while still catching any real content change to the English
source.

## Ownership

The owner is the person a drift report is addressed to. When `verify`
fails, it prints the stale section *and* the owner:

```
DRIFT    README.zh-Hans.md: section "install in 30 seconds" is stale: ...
OWNER    README.zh-Hans.md is kept current by @your-handle
```

A language with no entry in the owners table is reported as unowned:

```
OWNER    README.zh-Hans.md has no owner in [tool.bernstein.readme-l10n.owners]
```

That is a real state, not a warning to ignore. An unowned translation is
a removal candidate: it will drift, and the drift will land on whoever
edited the English source, who by definition cannot check it. The repo
carried eleven translations once, added in a single change with no gate
and no owners; seventeen days later they were removed as stale. The gate
prevents the silent part of that; the owners table prevents the rest.

Handing a language over is a one-line change to the table, made by the
person taking it on. Nobody is assigned a language they did not
volunteer for.
