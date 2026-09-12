## Central compliance control registry and suite control declaration enforcement

Bernstein now features a central compliance control registry (`bernstein.compliance.controls`) containing standard controls mapped across EU AI Act, OWASP ASI, OWASP Skills, NIST AI RMF, ISO/IEC 42001, and FINOS AIGF.

Every benchmark task suite (`BenchSuite`) must declare the control IDs it measures. Unmapped suites or suites declaring unregistered control IDs fail build validation (`validate_controls`).

Operators and auditors can inspect controls and benchmark coverage using `bernstein compliance controls [--coverage] [--framework <name>] [--format text|json|markdown]`.

The declaration is enforced where every `bench` subcommand resolves its
suite, so a suite that maps to no control cannot run, score, or publish a
bundle — built-in and `.json` suites alike. Both built-in suites now declare
controls: `golden-v1` (`CTL-ROB-01`, `CTL-EVAL-01`, `CTL-EVAL-02`,
`CTL-QUAL-02`) and `tool-surface-v1` (`CTL-SEC-02`, `CTL-SEC-05`,
`CTL-EVAL-01`).

A declared control set is part of suite identity — canonicalised, so order
and repeats do not matter — and both built-in suites now declare one, so
**both `golden-v1` and `tool-surface-v1` change `suite_hash`** in this
release. A bundle or reliability receipt produced against either on a
previous release will report a suite-hash mismatch under `bench verify` /
`bench reliability-verify` and needs re-running. A suite that declares no
controls hashes exactly as before.

Because a declaration is now required, a `.json` suite written by a previous
release is refused by every `bench` subcommand until a `controls` list is
added to the file; adding it changes that suite's hash, so bundles produced
against the old file need re-running too. The gate is applied at the CLI;
the `BenchRunner` / `ReliabilityRunner` library API does not enforce it.

The control table in `docs/compliance/regulator-mapped-packs.md` is pinned
by a test to what the registry renders for every built-in suite, so it
cannot drift from what the code declares; `bernstein compliance controls
--coverage` reads the same suite list (#5455).
