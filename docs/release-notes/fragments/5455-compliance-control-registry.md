## Compliance control registry and suite mapping

Added a unified compliance control registry in `bernstein.compliance.controls` covering
controls across the EU AI Act, OWASP Top 10 for Agentic Applications (ASI), OWASP Agentic Skills
Top 10 (AST), ISO/IEC 42001 Annex A, and the FINOS AI Governance Framework. Benchmark suites
now declare the control IDs they measure via `controls: [...]`, and `bernstein compliance controls`
surfaces the control catalog and framework cross-references (#5455).
