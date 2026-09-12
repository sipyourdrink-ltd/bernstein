# Governance

Bernstein is a single-maintainer project. [@chernistry](https://github.com/chernistry)
holds the final call. This page states how that call gets made, so
contributors do not have to infer it from the other files.

**How decisions are made.** In the open, in issues and pull requests.
There is no private channel where a proposal is really decided. The
maintainer decides and the reasoning goes in the thread next to the
decision — a "no" that does not say what would change it is an
incomplete answer. Boundaries already settled live in
[docs/scope.md](docs/scope.md), each pointing at the
[decision record](docs/decisions/index.md) behind it.

**Changes that need an issue first.** Agree on the design before writing
code when the change touches the public API (CLI commands and flags, HTTP
endpoints, the adapter ABC), a schema or on-disk format (`schemas/`,
`proto/`, the `.sdd/` state layout), or a security boundary (auth, tokens,
sandboxing, the audit chain, anything in the [SECURITY.md](SECURITY.md)
in-scope table). State the problem, the option chosen, and what it costs.
Everything else — bug fixes, docs, tests, a new adapter — goes straight
to a pull request.

**Becoming a committer or a core reviewer.** Both roles are defined in the
[review charter](docs/governance/review-charter.md#7-becoming-and-remaining-a-committer),
which sets an objective floor for committer; core reviewer has no numeric
floor, only a sustained record of reviews that found real problems. Anyone
may nominate anyone, including themselves, in an issue, and the maintainer
confirms. Area stewardship
([CONTRIBUTING.md](CONTRIBUTING.md#areas)) is the triage-level path that
usually comes first. Write access reaches the release workflows and their
publishing credentials, so it follows an established record rather than
arriving with a request. Both roles are recorded in
[.github/quorum-roster.toml](.github/quorum-roster.toml), which is the
roster the merge gate reads and the one the charter's lapse rule is
evaluated against; a core reviewer is additionally listed in
[CODEOWNERS](.github/CODEOWNERS), which is how GitHub requests them on the
paths they own, and GitHub only honors code owners who hold write. Adding
someone means both files. The project stays single-maintainer, as above.

**Licensing.** Apache-2.0 ([LICENSE](LICENSE)); by contributing you agree
your contributions are licensed under it. There is no CLA and no sign-off
requirement — opening the pull request is the whole ceremony. The project
name is not covered by the code grant, see [TRADEMARKS.md](TRADEMARKS.md).
