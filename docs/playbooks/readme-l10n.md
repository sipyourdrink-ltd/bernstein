# README translation gate (readme-l10n)

Every translated `README.<ietf-tag>.md` mirrors the English
`README.md` section for section. Each translated section carries an
HTML-comment binding to a content hash of the English section it
mirrors. `bernstein readme-l10n verify` recomputes those hashes and
fails on drift, naming the language and the exact stale section
heading - so a PR that edits an English section and leaves a
translation behind turns red in the same CI run.

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

A binding line for a section that does not exist in `README.md`
makes `sync` warn; a translated section without a binding line makes
`verify` fail with the section name.

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
dropped paragraph are indistinguishable from the outside - so joining
two translated paragraphs that are separate in English is an exit-1
failure, even though no content is missing. Split them back apart to
match the English layout.

## Hash stability

The hash input is normalised before hashing: trailing whitespace is
stripped per line, blank lines are dropped, lines are joined with
`\n`. This absorbs prettier/editor noise (trailing whitespace,
blank-line reflow) so the gate does not cry wolf on every formatting
pass, while still catching any real content change to the English
source.
