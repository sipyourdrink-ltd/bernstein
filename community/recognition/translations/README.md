# Translations of the recognition issue

One file per language, named by the code in `LANGUAGES` in
`scripts/contributor_recognition.py`: `hi.md`, `zh-Hans.md`, `vi.md`,
`es.md`, `pt.md`, `ru.md`, `uk.md`, `pl.md`.

Rules:

- Each file is a translation of `../ISSUE.md`, the English source. It adds
  nothing the English text does not say and leaves nothing out. Links,
  addresses and the `KEY=VALUE` lines stay exactly as in the source.
- The two placeholders (`{{LANGUAGE_SWITCHER}}`, `{{REPLY_BY}}`) are not
  translated and not included: the switcher lives in the English body only,
  and the reply deadline is stated in the English body.
- When the English source changes in substance, every file here changes in
  the same pull request. Nothing is published with the translations behind
  the source.
- Publication: `scripts/contributor_recognition.py publish-issue` posts one
  collapsible comment per file, in the order of `LANGUAGES`, then links each
  from the row under the title. `translations --out-dir` renders the same
  comment bodies locally for review.

Why these eight: the contributor population since June 2026, checked against
public profiles and repository evidence (the summary is in the pull request
that added this folder). Adding a language is one row in `LANGUAGES` and one
file here; it should follow a real cluster of contributors, not the size of
the language.
