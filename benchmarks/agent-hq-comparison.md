# Bernstein vs GitHub Agent HQ: Comparison Notes

This comparison page is intentionally qualitative.

Bernstein is not publishing estimated or simulated benchmark tables against Agent HQ. Public benchmark claims are currently limited to verified SWE-Bench Lite eval artifacts produced by `benchmarks/swe_bench/run.py eval`, and v1 scope is Bernstein vs solo baselines.

## Where Bernstein is stronger

- model and provider flexibility
- local control over orchestration logic
- file-based state and inspectable traces
- compatibility with non-GitHub repos and arbitrary CLI agents

## Where Agent HQ is stronger

- GitHub-native workflow integration
- zero local setup
- managed platform experience for issue-to-PR flows
- enterprise support and procurement path

## Benchmark policy

- Internal modeling harnesses such as `benchmarks/run_benchmark.py` are not public benchmark evidence.
- Bernstein will only publish public benchmark numbers from verified SWE-Bench eval artifacts with provenance metadata.
- Agent HQ stays on the qualitative side of the comparison until Bernstein can reproduce it under a Bernstein-owned live harness.
