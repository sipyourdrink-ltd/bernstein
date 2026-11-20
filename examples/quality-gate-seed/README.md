# Inspect a quality gate seed

Read [bernstein.yaml](bernstein.yaml) with Bernstein's seed parser and print the
resulting `QualityGatesConfig` settings. This is a parse-only example and runs
offline with the project dependencies installed.

```bash
python examples/quality-gate-seed/demo.py
```

The JSON output includes these values from the example seed.

| Setting | Parsed value |
| --- | --- |
| `mutation_testing` / `mutation_threshold` | `true` / `0.75` |
| `large_file_check` / `large_file_threshold` | `true` / `800` |
| `test_expansion` | `true` |
| `intent_verification.enabled` / `intent_verification.max_tokens` | `true` / `128` |
| `intent_verification.block_on_partial` | `true` |

The nested `intent_verification` object also includes parser defaults for
omitted settings, such as `fork_from_context: null`. To inspect another
seed, pass its path as the argument.

```bash
python examples/quality-gate-seed/demo.py path/to/bernstein.yaml
```

For example, `quality_gates.timeout_s` takes an integer. Setting it to
`true` exits with status 2 and identifies the invalid field.
