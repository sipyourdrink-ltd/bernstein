# Governance

Bernstein is a single-maintainer project. [@chernistry](https://github.com/chernistry)
holds the final call. This page states how that call gets made, so
contributors do not have to infer it from the other files.

**How decisions are made.** A contributor's proposal or pull request is
decided in its public issue or pull request; there is no private channel
where it is really decided. The
maintainer decides and the reasoning goes in the thread next to the
decision — a "no" that does not say what would change it is an
incomplete answer. Boundaries already settled live in
[docs/scope.md](docs/scope.md), each pointing at the
[decision record](docs/decisions/index.md) behind it.

**Where the maintainer plans.** The maintainer keeps work orders and
research notes in a private planning tracker and decides there what to
build next, so most planned work does not appear as a public issue
before its pull request. Every change lands through a public pull request
open to review. The [review charter](docs/governance/review-charter.md)
sets the approval requirements for its author and the paths it touches.

Ordinary bug reports, proposals from
contributors, the labelled queues in [CONTRIBUTING.md](CONTRIBUTING.md)
and every issue that someone has claimed or has a pull request for stay
in the public tracker. Report suspected vulnerabilities privately using
the channels in [SECURITY.md](SECURITY.md).

**Changes that need an issue first.** Agree on the design before writing
code when the change touches the public API (CLI commands and flags, HTTP
endpoints, the adapter ABC), a schema or on-disk format (`schemas/`,
`proto/`, the `.sdd/` state layout), or a security boundary (auth, tokens,
sandboxing, the audit chain, anything in the [SECURITY.md](SECURITY.md)
in-scope table). Before writing code, open a public issue stating the
problem, the option chosen and what it costs.
Other changes, including ordinary bug fixes, docs, tests and new adapters,
go straight to a pull request.

When a fix involves a suspected vulnerability, coordinate it privately
with the maintainer under [SECURITY.md](SECURITY.md). A public design
issue can describe the general boundary change while the report and fix
stay private until disclosure. If even that description would reveal the
vulnerability, agree on safe wording or a disclosure date in private.
Implementation of the boundary change starts once the required issue
can be published safely and is open.

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

**Roles below the maintainer.** Triager and area reviewer sit beside committer
and core reviewer, each with a term and a stated limit; what each may do, how it
is granted and how it lapses is in
[Roles and terms](docs/governance/review-charter.md#11-roles-and-terms). The
roster stays maintainer-only: nobody else edits it, and org ownership,
rulesets, secrets and the publisher identities stay with the maintainer.

**Election.** *Proposed, to be confirmed by the maintainer:* roles are filled
in an election, held around October. Anyone with a merged pull request in the
last 90 days is eligible. The maintainer opens a nomination window of at least
seven days in an issue; anyone may
nominate an eligible person, themselves included, and people may speak for or
against in the thread. When the window closes the maintainer confirms names by
roster pull request. The maintainer has a veto and can decline any nomination,
with the reason in the thread; the election informs the decision, it does not
bind it.

**Licensing.** Apache-2.0 ([LICENSE](LICENSE)); by contributing you agree
your contributions are licensed under it. There is no CLA and no sign-off
requirement — opening the pull request is the whole ceremony. The project
name is not covered by the code grant, see [TRADEMARKS.md](TRADEMARKS.md).

## Temporary merge rule until 23 November 2026

From the merge of this change until 23 November 2026, the maintainer may
merge a contributor pull request once the required checks are green and
an independent automated review has passed on the exact head commit,
without the second approval. This temporary rule supports release
stabilisation before v4.0.0.

From 23 November 2026, the normal approval rule applies again. A revert
pull request is prepared alongside this change for 23 November 2026.
