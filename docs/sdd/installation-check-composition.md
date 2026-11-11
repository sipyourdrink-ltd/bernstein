# Installation check composition

Installation findings share their producers with `bernstein doctor`. They are
composed by the CLI because the legacy diagnostic helpers belong to the CLI.

`src/bernstein/cli/commands/govern_audit_cmd.py` owns the stable
`INSTALLATION_CHECKS` catalog, `DoctorInstallationAdapter`, and
`populate_installation_checks`. The audit command explicitly populates this
catalog after populating the core checks. Repeating composition does not
register a check twice. Listing the catalog does not execute diagnostic probes.

`src/bernstein/core/checks/adapters.py` owns the observation-to-`Finding`
conversion and producer decorator. It accepts observations or callbacks and
does not import CLI implementations. The core default registry continues to
populate compliance, conditional sentinel, and duplication checks independently
of the installation catalog. Installation checks are opt-in through `--only INS`.

Doctor JSON and installation audit JSON serialize findings through the shared
`Finding.to_dict` contract, including the reason for a verdict. Legacy doctor
display rows and readiness behavior remain separate from these audit verdicts.
Unavailable probes produce `not_measurable` findings rather than passing ones.

The CLI catalog test pins INS-001 through INS-020 and verifies idempotence,
core independence, and default omission. The installation tests check that
listing executes no probes and compare doctor and audit findings for the same
observations. Import-linter enforces the core-to-CLI dependency boundary.

See the [doctor operations guide](../operations/doctor.md) for commands, the
stable IDs, and the installation-specific measurement rules.
