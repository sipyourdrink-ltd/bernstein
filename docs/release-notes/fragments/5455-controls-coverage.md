## Compliance control-to-suite coverage and latest measurements

`bernstein compliance controls` now surfaces the declaring benchmark suites and
latest recorded measurement for each registry control across text, Markdown,
and JSON outputs (#5455, #7098):

- Built-in benchmark suite control declarations are exposed from a single shared
  mapping in `bernstein.eval.bench.bench_cli`, paired with tests verifying every
  registered built-in suite declares at least one valid registry control ID.
- `bernstein.compliance.control_coverage` joins the central control registry
  with built-in (and optional custom `.suite.json`) declarations and
  `SubmissionBundle` records, selecting the latest measurement per suite
  deterministically regardless of input order and reporting `unavailable` when no
  measurement exists.
- `bernstein compliance controls` accepts optional repeatable `--bundle` and
  `--bundle-dir` flags (falling back to `.sdd/bench` / `.sdd/benchmarks` when
  omitted) while preserving existing control keys and stable control ordering.
