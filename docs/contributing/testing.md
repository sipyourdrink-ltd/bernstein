# Testing & CI Hardening Reference

This page documents the test tooling we run on every PR and overnight,
what bug class each tool catches, and how to reproduce a CI failure
locally without waiting for the cloud runner.

> **Docs duty for test PRs.** Any PR that adds a new test layer, tool, or
> harness MUST update this page in the same PR. See the project-wide
> [Documentation duty](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CONTRIBUTING.md#docs-alongside-code) rule.

## Tool ↔ bug-class matrix

| Tool                        | Bug class                                                         | When                |
| --------------------------- | ----------------------------------------------------------------- | ------------------- |
| **Hypothesis** (property)   | Hash-chain breaks, signature roundtrip, canonical-bytes drift     | PR (smoke), nightly (deep)  |
| **Schemathesis**            | 5xx leaks against fuzzed REST inputs                              | PR (allow-list), nightly (full)  |
| **CrossHair**               | Logic errors in pure helpers (concolic execution, assert checks)  | nightly only         |
| **mutmut diff-only**        | Test-effectiveness gaps on PR-changed lines                       | PR (advisory)       |
| **mutmut fixed paths**      | Per-module kill-rate gate on a fixed critical-path module list    | PR (path-filtered) + weekly cron  |
| **mutmut full**             | Test-effectiveness gaps across the whole repo                     | nightly (advisory)  |
| **Semgrep** (custom rules)  | eval/exec/pickle in production, env-leak in `_spawn_*`            | PR (ERROR fails)    |
| **Bandit**                  | Generic Python security smells (shell=True, weak hash, tarfile)   | PR (HIGH only)      |
| **pip-audit**               | Known PyPI CVEs in production deps                                | PR (strict)         |
| **Beartype** (claw)         | Runtime type-contract violations on public security/cluster APIs  | PR                  |
| **syrupy** (snapshot)       | JSONL/audit/lineage wire-format drift                             | PR                  |
| **Pyright strict zone**     | Untyped/implicit-Any leakage in `core/security/`, `core/protocols/cluster/` | PR                  |
| **mypy strict zone**        | Same, from mypy's inference, in `core/{evidence,identity,lineage,persistence}/` | PR                  |
| **Vulture**                 | Dead code (unused functions/classes/vars at confidence ≥80)       | PR                  |
| **diff-cover** (LEVEL 1)    | Changed lines below the committed diff-coverage floor             | PR (advisory)       |
| **coverage ratchet** (LEVEL 2) | Total coverage dropped below the committed high-water mark      | push to main (advisory) |
| **import-linter**           | Architecture-contract violations (cross-package imports)          | PR                  |
| **No-network guard**        | Unit tests that open a real outbound connection (flaky by design) | PR (every unit run) |
| **ruff** + **typos**        | Lint, format drift, common typos                                  | PR                  |

## Run any of the above locally

```bash
# Property suite (smoke)
HYPOTHESIS_PROFILE=smoke uv run pytest tests/property/ -q --no-cov

# Property suite (deep - same as nightly)
HYPOTHESIS_PROFILE=deep uv run pytest tests/property/ -q --no-cov

# Snapshot tests
uv run pytest tests/snapshot/ -q --no-cov
# Update snapshots after an intentional schema change:
uv run pytest tests/snapshot/ -q --no-cov --snapshot-update

# Schemathesis (smoke - only the critical-surface allow-list)
BERNSTEIN_AUTH_DISABLED=1 SCHEMATHESIS_PROFILE=smoke \
  uv run pytest tests/contract/ -q --no-cov

# Semgrep (project rules; ERROR severity is the PR gate).
# Install once via `uv tool install semgrep` - semgrep's transitive
# pins (click<8.2, opentelemetry-sdk<1.26) conflict with our project
# floors, so it lives in its own venv outside `uv sync`.
uv tool install semgrep
uv tool run semgrep --config .semgrep.yml --severity ERROR --error src/

# Bandit (production HIGH-only with baseline)
uv run bandit -r src/ -ll --severity-level high -b .bandit-baseline.json

# pip-audit
uv run pip-audit --strict

# Beartype claw - runs the focused unit tests under runtime type
# enforcement on core.security + core.agents + core.protocols.cluster
BEARTYPE_USE_CLAW=enable \
  uv run pytest tests/unit/ -q --no-cov \
  -k 'security or agent or cluster or audit or lineage'

# Pyright strict zone
uv run pyright --typecheckingmode strict \
  src/bernstein/core/security/ \
  src/bernstein/core/protocols/cluster/

# mypy strict zone (gated). Widen it by removing entries from `exclude`
# in mypy.gate.ini as each module reaches strict cleanliness.
uv run mypy --config-file mypy.gate.ini

# mypy repo-wide (advisory - settings in pyproject.toml [tool.mypy]).
# Carries a pre-existing backlog; the CI job only fails here if the run
# aborts before checking the tree.
uv run mypy src

# Vulture
vulture src/ vulture_whitelist.py --min-confidence 80 --exclude tests,docs

# Diff-cover (after a coverage run). The floor is the committed
# diff_coverage_floor_percent in .coverage-baseline.json (LEVEL 1 of the
# coverage ratchet); the weekly bump nudges it up. See
# docs/operations/coverage-ratchet.md.
uv run pytest tests/unit/ --cov=src/bernstein --cov-report=xml
FLOOR=$(uv run python scripts/coverage_ratchet.py show-floor --baseline .coverage-baseline.json)
uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under="$FLOOR"

# Total-coverage ratchet (LEVEL 2): compare a coverage.xml total to the
# committed high-water mark without writing unless it rose.
uv run python scripts/coverage_ratchet.py check \
  --coverage-xml coverage.xml --baseline .coverage-baseline.json --no-bump
```

## When a tool fires on you

### Semgrep ERROR
The rule is intentionally tight. If you genuinely need the pattern,
add the inline `# nosemgrep: <rule-id>  -- <one-line justification>`
comment. PRs that disable a rule without justification get bounced.

### Bandit HIGH
Only HIGH fails the PR; the 11 pre-existing HIGH findings on `main`
are captured in `.bandit-baseline.json`. New HIGH findings need either
a fix or an explicit baseline update with rationale in the PR
description.

### Hypothesis falsifying example
The error output includes a `git apply .hypothesis/patches/...` line.
Apply that patch to add the failing example as a deterministic
regression case, fix the bug, and the patch becomes a permanent unit
test.

### Schemathesis 5xx leak
A real bug - an endpoint should never propagate an unhandled
exception. The reproducer is printed at the bottom of the failure (a
`curl` invocation against the mounted ASGI app).

### Snapshot diff
If the diff is intentional (you changed an audit field on purpose),
re-run with `--snapshot-update` and commit the updated `.ambr`. If
not, you've caused unintended wire-format drift.

### mutmut survivor
The mutation operator changed `==` to `!=` (etc.) and no test
caught it. Either add a test that distinguishes the two operators
or, if the mutation is genuinely undetectable (e.g. an off-by-one
in a comment-only path), document why in `mutmut_config.py`.

### mutmut fixed-paths gate
`mutation-fixed.yml` runs `scripts/mutmut_critical.py` against a
fixed list of high-risk modules (atomic claim, HMAC audit chain,
audit integrity verifier, lineage v1 trio, seed parser) and gates
on a per-module kill rate. The module list, per-module thresholds,
and wall-clock budgets live in `scripts/mutmut_critical.py:MODULES`.

The gate is **advisory** while thresholds calibrate (the matrix job
sets `continue-on-error: true`). The PR comment posted by the
workflow summarises each module's score and survivors; until the
gate is flipped to enforcing, treat a red row as a follow-up TODO,
not a merge blocker.

Reproduce locally:

```bash
# All modules (slow - budgets sum to about an hour).
uv run python scripts/mutmut_critical.py

# One module:
uv run python scripts/mutmut_critical.py --only claim_next
uv run python scripts/mutmut_critical.py --list   # show keys
```

Adding a module to the gate: extend `MODULES` in
`scripts/mutmut_critical.py`, mirror the matrix in
`.github/workflows/mutation-fixed.yml`, and add the source/test
paths to the `paths:` filter on the same workflow.

## Hermetic unit tests (no network)

Unit tests are hermetic: a test under `tests/unit/` must not open a real
outbound network connection. A test that talks to a remote host passes only
while that host answers and fails intermittently otherwise (a transient 404, a
DNS hiccup, a rate limit), turning a green suite red for reasons unrelated to
the change under test. One such test (a signed-catalog install whose fixture
pointed at a `github://` URL) once reached a live host, 404'd intermittently in
CI, and wedged the merge queue.

### The guard

`tests/unit/conftest.py` installs an **autouse** fixture that wraps
`socket.socket.connect` / `socket.socket.connect_ex` for every unit test (logic
in `tests/unit/_no_network.py`). Any attempt to connect to a non-loopback
address raises immediately:

```
RuntimeError: unit tests must not touch the network: blocked connection to
api.example.com:443. Mock it (see docs/contributing/testing.md), or move a
genuine integration test to tests/integration/.
```

Because the patch sits at the socket layer, it covers every higher-level client
(`http.client`, `urllib`, `requests`, `httpx`, raw sockets) without per-library
patching. It is hand-rolled rather than a third-party plugin so it adds no
dependency to vet, lock, and audit, and so it integrates with the suite's
existing strict-marker opt-out convention.

**Loopback stays allowed.** Connections to `127.0.0.0/8`, `::1`, and the
literal hostname `localhost`, plus Unix-domain sockets, pass through untouched,
so the many unit tests that spin a local mock server keep working. The guard
inspects the literal target before any name resolution, so a loopback hostname
is allowed without an egress; a non-loopback hostname is blocked at the
resolution boundary.

The scope is `tests/unit/` only. Integration tests (`tests/integration/`)
run real servers and are not guarded, because their conftest does not install
the fixture.

### When the guard fires on you

The test under `tests/unit/` opened a real connection. Two correct fixes, in
order of preference:

1. **Make it hermetic (almost always the right answer).** Mock the network at
   the seam: inject a fake client, patch the transport, or use the
   `respx`/`TestClient` patterns already in the suite. Do **not** "fix" it by
   pointing the call at a loopback URL unless the test genuinely runs a local
   server.
2. **Move a genuine integration test to `tests/integration/`.** If the test
   truly must reach the network, it is not a unit test; relocate it. The guard
   does not apply there.

### Opting a single test out (rare)

For the rare case where a test must stay under `tests/unit/` and reach the
network, mark it and document why:

```python
import pytest


@pytest.mark.allow_network  # justification: probes the real X endpoint; see #NNNN
def test_live_thing() -> None:
    ...
```

The marker is registered in `pyproject.toml` (`--strict-markers` is on, so an
unregistered marker is itself an error). Prefer relocation to
`tests/integration/` over the marker: a network-touching test in the unit suite
is a flake waiting to happen.

### Reproduce locally

```bash
# Full unit suite with the guard active (per-file isolated runner):
uv run python scripts/run_tests.py --parallel 4

# Or straight pytest:
uv run pytest tests/unit/ -q --no-cov

# The guard's own meta-tests:
uv run pytest tests/unit/test_no_network_guard.py -q --no-cov
```

## Multi-adapter pentest fan-out

The `security-pentest` eval scenario supports a fan-out entry point
that runs the same fixture through N adapters in parallel and reports
the per-adapter and aggregate consensus precision/recall split:

```python
from bernstein.eval.pentest_runner import (
    load_scenario_config,
    mock_adapter,
    run_multi_adapter,
)
from bernstein.eval.pentest_scorer import PentestScorer

config = load_scenario_config(Path("eval/scenarios/security_pentest.yaml"))
report = run_multi_adapter(
    adapters={"alpha": mock_adapter, "beta": mock_adapter},
    config=config,
)
split = PentestScorer().score_multi(report)
print(split.per_adapter["alpha"].precision, split.per_adapter["alpha"].recall)
print(split.consensus.precision, split.consensus.recall)
```

The CLI exposes the same surface:

```bash
bernstein eval scenario security-pentest --adapters mock,mock
```

Determinism contract: the per-adapter call order is recorded on the
report (`call_order`) and the consensus list is sorted on the dedup
key `(canonical_vuln_type, normalized_path)`, never on adapter
completion order. Two runs over the same input therefore produce
byte-identical output.

Degenerate case: passing a single adapter to `run_multi_adapter`
produces a per-adapter result that matches the legacy `run_scenario`
output exactly - existing scripts keep working unchanged.

## CI cost budget

PR-time CI must stay under 2× the pre-2026 baseline. Heavy work
(full mutmut, deep Hypothesis, full Schemathesis, full CrossHair)
runs only in `nightly-deep-tests.yml` (cron `0 3 * * *`) and is
explicitly `continue-on-error` so an overnight regression doesn't
block tomorrow's PRs.

The added PR-time jobs target ≤8 min wall-clock each and run in
parallel after the lint job clears (so a typo PR fails fast in <2
min without burning compute on the heavy stack).

## Sharded unit suite

`scripts/run_tests.py` runs each `tests/unit/test_*.py` file in its own
subprocess (the OOM-avoidance model: a single `pytest --cov` over all
files in one process exceeds the 7 GB runner ceiling). Each subprocess
pays a fixed ~2.7 s of Python startup + full-package import regardless
of how many tests the file holds, so at ~1.4k files a single runner
spent most of its wall time on startup churn rather than test
execution.

The `Test` job therefore fans the file list out across parallel
runners with `--shard i/N`:

```
# Run only shard 1 of 4 (a deterministic, disjoint quarter of the files):
uv run python scripts/run_tests.py --shard 1/4

# Compose with the affected-only selection used on PRs:
uv run python scripts/run_tests.py --shard 1/4 --affected origin/main
```

The partition is **position-modulo over the sorted file list**: shard
`i` owns every file whose index `j` satisfies `j % N == i - 1`. That
makes it deterministic and stable (a failing shard reruns the identical
slice), complete and disjoint (the union of all `N` shards is exactly
the full list, no file runs twice), and balanced (shard sizes differ by
at most one). An empty shard (when `N` exceeds the file count) is a
legitimate no-op that exits 0.

In CI the `ubuntu`/`windows` `Test` cells fan out across a `shard`
matrix dimension; the rolled-up `needs.test.result` the `CI gate`
aggregator reads is `failure` if *any* shard cell fails, so every shard
is still required. Coverage / JUnit / Codecov upload runs on shard 1
only (its own file loop still covers every file, so the pin
deduplicates without narrowing coverage). The `macos` cell keeps a
single literal job name (branch-protection required-context); it runs a
deterministic `--shard 1/4` subset on push and the affected slice on
PRs, with `ci-macos-nightly.yml` running the full macOS matrix daily as
the safety net.
