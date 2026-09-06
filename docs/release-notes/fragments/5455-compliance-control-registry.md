## Central compliance control registry and suite control declaration enforcement

Bernstein now features a central compliance control registry (`bernstein.compliance.controls`) containing standard controls mapped across EU AI Act, OWASP ASI, OWASP Skills, NIST AI RMF, ISO/IEC 42001, and FINOS AIGF.

Every benchmark task suite (`BenchSuite`) must declare the control IDs it measures. Unmapped suites or suites declaring unregistered control IDs fail build validation (`validate_controls`).

Operators and auditors can inspect controls and benchmark coverage using `bernstein compliance controls [--coverage] [--framework <name>] [--format text|json|markdown]`.
