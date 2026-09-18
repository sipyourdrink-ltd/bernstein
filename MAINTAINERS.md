# Maintainers

| Maintainer | Areas | Reach them at |
|---|---|---|
| [@chernistry](https://github.com/chernistry) | Everything: core orchestration, adapters, docs, packaging, CI | [Issues](https://github.com/sipyourdrink-ltd/bernstein/issues) and [Discussions](https://github.com/sipyourdrink-ltd/bernstein/discussions) |

[.github/CODEOWNERS](.github/CODEOWNERS) is the authoritative per-path list
and is what GitHub actually uses to route review. This table is the human
summary; if the two disagree, CODEOWNERS wins.

The areas open to stewardship are adapters, the web dashboard, the terminal
UI, docs, and packaging — see [GOVERNANCE.md](GOVERNANCE.md) for how a name
gets onto this table, and [CONTRIBUTING.md](CONTRIBUTING.md#areas) for the
mechanics.

## Review roster

Approvals on a pull request are counted against
[.github/quorum-roster.toml](.github/quorum-roster.toml). That file is the
authoritative list; this table is its human-readable mirror, and if the two
disagree the TOML wins. How the count works is in
[docs/governance/review-charter.md](docs/governance/review-charter.md).

| Role | Who | What it means on a pull request |
|---|---|---|
| Maintainer | [@chernistry](https://github.com/chernistry) | Own changes merge without approvals; approves protected paths and changes over 1,000 lines |
| Core reviewers | [@vaibhav8a](https://github.com/vaibhav8a), [@thegoodengineer](https://github.com/thegoodengineer), [@tenequm](https://github.com/tenequm), [@Phoenix1504e](https://github.com/Phoenix1504e) | At least one of the two approvals a contributor's change needs comes from here |
| Committers | [@Chirag6722](https://github.com/Chirag6722), [@Silentpartnercoding](https://github.com/Silentpartnercoding) | Approvals count toward the two; a standing "changes requested" blocks a merge, the maintainer's own included |
| Automation | `bernstein-the-conductor[bot]`, `renovate[bot]`, `dependabot[bot]` | Own changes merge on green CI, except on sensitive paths, where the maintainer approves |

A change to the roster is a pull request against the TOML file, following
[GOVERNANCE.md](GOVERNANCE.md); this table is updated in the same pull request.

Security reports do not go here. Use the channels in [SECURITY.md](SECURITY.md).
