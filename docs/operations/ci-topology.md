# GitHub Actions workflow topology

<!-- AUTO-GENERATED: run `uv run python scripts/gen_workflow_topology.py --update` to refresh -->

This report lists the workflow graph surfaces reviewers need to inspect when CI topology changes.

Drift on `main` self-heals: `.github/workflows/ci-topology-heal.yml` regenerates this report
after workflow changes merge and opens a squash auto-merge PR when the committed copy is stale.

## Workflow Summary

| Workflow | Name | Triggers | Concurrency | Jobs |
| --- | --- | --- | --- | --- |
| .github/workflows/a2a-federation-e2e.yml | a2a-federation-e2e | pull_request, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "a2a-federation-e2e-${{ github.ref }}"} | 1 |
| .github/workflows/adapter-conformance-canary.yml | Adapter conformance canary | schedule, workflow_dispatch | {"cancel-in-progress": "false", "group": "adapter-conformance-canary"} | 1 |
| .github/workflows/adapter-contract-drift.yml | Adapter contract drift | merge_group, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "adapter-contract-drift-${{ github.ref }}"} | 2 |
| .github/workflows/airgap-e2e.yml | Airgap E2E | pull_request, push, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "airgap-e2e-${{ github.ref }}"} | 1 |
| .github/workflows/auto-heal.yml | Auto-heal v2 | workflow_call | - | 2 |
| .github/workflows/auto-release.yml | Auto-release | workflow_call | - | 5 |
| .github/workflows/bernstein-ci-fix.yml | Bernstein CI Fix | workflow_call | - | 4 |
| .github/workflows/bernstein-issues-decompose.yml | Bernstein Issue Decompose | issues | {"cancel-in-progress": "true", "group": "bernstein-decompose-${{ github.event.issue.number }}"} | 4 |
| .github/workflows/bernstein-pr-review.yml | Bernstein PR Review | pull_request | {"cancel-in-progress": "true", "group": "bernstein-pr-${{ github.event.pull_request.number }}"} | 1 |
| .github/workflows/bisect-on-red.yml | Bisect on Red | workflow_call | - | 1 |
| .github/workflows/branch-protection-audit.yml | Branch protection audit | schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "branch-protection-audit-${{ github.ref }}"} | 1 |
| .github/workflows/ci-gate-stub.yml | CI gate stub | pull_request | {"cancel-in-progress": "true", "group": "ci-gate-stub-${{ github.event.pull_request.number \|\| github.ref }}"} | 2 |
| .github/workflows/ci-macos-nightly.yml | CI (macOS nightly) | push, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "ci-macos-nightly-${{ github.workflow }}-${{ github.ref }}"} | 2 |
| .github/workflows/ci-topology-heal.yml | CI topology heal | push, workflow_dispatch | {"cancel-in-progress": "true", "group": "ci-topology-heal"} | 1 |
| .github/workflows/ci-weekly-digest.yml | CI Weekly Digest | schedule, workflow_dispatch | {"cancel-in-progress": "false", "group": "ci-weekly-digest"} | 1 |
| .github/workflows/ci.yml | CI | merge_group, pull_request, push, workflow_dispatch | {"cancel-in-progress": "${{ github.event_name == 'pull_request' }}", "group": "ci-${{ github.workflow }}-${{ github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number) \|\| format('branch-{0}-{1}', github.ref, github.sha) }}"} | 32 |
| .github/workflows/cifuzz-pr.yml | CIFuzz (ClusterFuzzLite, PR) | schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "cifuzz-pr-${{ github.ref }}"} | 1 |
| .github/workflows/cleanup-runs.yml | Cleanup Action Runs | workflow_dispatch | {"cancel-in-progress": "false", "group": "cleanup-runs-${{ github.ref }}"} | 1 |
| .github/workflows/cluster-e2e.yml | cluster-e2e | pull_request, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "cluster-e2e-${{ github.ref }}"} | 1 |
| .github/workflows/cluster-tunnel-e2e.yml | cluster-tunnel-e2e | schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "cluster-tunnel-e2e-${{ github.ref }}"} | 1 |
| .github/workflows/code-review-bots-ci.yml | Code review bots (CLI lane) | pull_request | {"cancel-in-progress": "true", "group": "code-review-bots-${{ github.event.pull_request.number \|\| github.ref }}"} | 1 |
| .github/workflows/codeql.yml | CodeQL Security Analysis | pull_request, push, schedule | {"cancel-in-progress": "${{ github.event_name == 'pull_request' }}", "group": "codeql-${{ github.ref }}"} | 1 |
| .github/workflows/contract-drift-autofix.yml | Contract Drift Autofix | pull_request | {"cancel-in-progress": "true", "group": "contract-drift-${{ github.event.pull_request.number }}"} | 1 |
| .github/workflows/coverage-ratchet-weekly.yml | Coverage ratchet (weekly floor bump) | schedule, workflow_dispatch | {"cancel-in-progress": "false", "group": "coverage-ratchet-weekly"} | 1 |
| .github/workflows/coverage-ratchet.yml | Coverage ratchet (total) | push | {"cancel-in-progress": "false", "group": "coverage-ratchet"} | 1 |
| .github/workflows/dependabot-auto-merge.yml | Dependabot Auto-merge | pull_request | {"cancel-in-progress": "true", "group": "dependabot-merge-${{ github.event.pull_request.number }}"} | 1 |
| .github/workflows/dependency-review.yml | Dependency Review | pull_request | {"cancel-in-progress": "true", "group": "dependency-review-${{ github.event.pull_request.number \|\| github.ref }}"} | 1 |
| .github/workflows/docs-drift.yml | docs-drift | pull_request, push, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "docs-drift-${{ github.ref }}"} | 1 |
| .github/workflows/docs-observability-snapshot.yml | Observability snapshot | workflow_dispatch | {"cancel-in-progress": "false", "group": "docs-observability-snapshot"} | 1 |
| .github/workflows/eval-nightly.yml | eval-nightly | schedule, workflow_dispatch | {"cancel-in-progress": "false", "group": "eval-nightly-${{ github.ref }}"} | 3 |
| .github/workflows/hotfix-r-tracker.yml | Hotfix R-counter | push | {"cancel-in-progress": "false", "group": "hotfix-r-tracker-${{ github.sha }}"} | 1 |
| .github/workflows/license-compliance.yml | License Compliance | pull_request, push | {"cancel-in-progress": "true", "group": "license-${{ github.ref }}"} | 1 |
| .github/workflows/main-sha-marker.yml | Main SHA marker | push | {"cancel-in-progress": "false", "group": "main-sha-marker-${{ github.sha }}"} | 1 |
| .github/workflows/mutation-fixed.yml | Mutation (fixed critical paths) | merge_group, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "mutation-fixed-${{ github.workflow }}-${{ github.ref }}"} | 2 |
| .github/workflows/nightly-canary.yml | Nightly real-run canary | schedule, workflow_dispatch | {"cancel-in-progress": "false", "group": "nightly-canary"} | 1 |
| .github/workflows/nightly-deep-tests.yml | Nightly deep tests | schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "nightly-deep-tests"} | 8 |
| .github/workflows/nightly-drift-sweep.yml | Nightly drift sweep | schedule, workflow_dispatch | {"cancel-in-progress": "false", "group": "nightly-drift-sweep"} | 1 |
| .github/workflows/pentest.yml | Adversarial Pen-Test Suite | workflow_dispatch | {"cancel-in-progress": "false", "group": "pentest-${{ github.ref }}"} | 1 |
| .github/workflows/post-ci-dispatcher.yml | Post-CI dispatcher | workflow_run | {"cancel-in-progress": "false", "group": "post-ci-dispatcher-${{ github.event.workflow_run.head_sha }}"} | 5 |
| .github/workflows/pr-labels.yml | PR labels | pull_request_target | {"cancel-in-progress": "true", "group": "pr-labels-${{ github.event.pull_request.number }}"} | 1 |
| .github/workflows/pr-observability-summary.yml | PR observability summary | pull_request, workflow_dispatch | {"cancel-in-progress": "true", "group": "pr-observability-${{ github.event.pull_request.number \|\| github.event.inputs.pr_number }}"} | 1 |
| .github/workflows/pr-policy.yml | PR policy | pull_request | {"cancel-in-progress": "true", "group": "pr-policy-${{ github.event.pull_request.number }}"} | 1 |
| .github/workflows/publish-docker.yml | Publish Docker Image | release, workflow_dispatch | {"cancel-in-progress": "false", "group": "publish-docker-${{ github.ref }}"} | 1 |
| .github/workflows/publish-extension.yml | Publish VS Code Extension | push, release | {"cancel-in-progress": "false", "group": "publish-extension-${{ github.ref }}"} | 1 |
| .github/workflows/publish-homebrew.yml | Publish Homebrew Formula | release, workflow_dispatch | {"cancel-in-progress": "false", "group": "publish-homebrew-${{ github.ref }}"} | 1 |
| .github/workflows/publish.yml | Publish | push, workflow_dispatch | - | 8 |
| .github/workflows/reconcile-release.yml | Reconcile release drift | schedule, workflow_dispatch | {"cancel-in-progress": "false", "group": "reconcile-release"} | 1 |
| .github/workflows/release-major-minor.yml | Major/Minor Release | workflow_dispatch | {"cancel-in-progress": "false", "group": "release-major-minor-${{ github.ref }}"} | 1 |
| .github/workflows/required-check-canary.yml | Required-check name canary | pull_request, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "required-check-canary-${{ github.event.pull_request.number \|\| github.ref }}"} | 1 |
| .github/workflows/review-bot-ack.yml | Review-bot acknowledgement gate | merge_group, pull_request, pull_request_review | {"cancel-in-progress": "true", "group": "review-bot-ack-${{ github.event.pull_request.number \|\| github.ref }}"} | 2 |
| .github/workflows/review-bot-sweep.yml | Review-bot post-merge sweep | schedule, workflow_dispatch | - | 1 |
| .github/workflows/sbom.yml | SBOM | release, workflow_dispatch | {"cancel-in-progress": "false", "group": "sbom-${{ github.ref }}"} | 1 |
| .github/workflows/scorecard.yml | OSSF Scorecard | branch_protection_rule, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "scorecard-${{ github.ref }}"} | 2 |
| .github/workflows/soc2-evidence-nightly.yml | soc2-evidence-nightly | schedule, workflow_dispatch | {"cancel-in-progress": "false", "group": "soc2-evidence-${{ github.ref }}"} | 2 |
| .github/workflows/spiffe-extra-e2e.yml | SPIFFE Extra E2E | pull_request, push, workflow_dispatch | {"cancel-in-progress": "true", "group": "spiffe-extra-e2e-${{ github.ref }}"} | 1 |
| .github/workflows/stale.yml | Stale cleanup | schedule | {"cancel-in-progress": "false", "group": "stale-${{ github.ref }}"} | 1 |
| .github/workflows/static-analysis-extended.yml | static-analysis (extended) | merge_group, push, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "static-analysis-extended-${{ github.ref }}"} | 6 |
| .github/workflows/trufflehog.yml | trufflehog (secret scanning) | pull_request, push, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "trufflehog-${{ github.ref }}"} | 1 |
| .github/workflows/trunk-health-slo.yml | Trunk Health SLO | schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "trunk-health-slo"} | 1 |
| .github/workflows/typecheck-ts.yml | TypeScript typecheck | pull_request, push | {"cancel-in-progress": "true", "group": "typecheck-ts-${{ github.event.pull_request.number \|\| github.ref }}"} | 1 |
| .github/workflows/zizmor.yml | zizmor (workflow static analysis) | pull_request, push, schedule, workflow_dispatch | {"cancel-in-progress": "true", "group": "zizmor-${{ github.ref }}"} | 1 |

## Check Emitters

| Workflow | Checks |
| --- | --- |
| .github/workflows/a2a-federation-e2e.yml | a2a-federation-e2e: a2a-federation-e2e (${{ matrix.os }}) |
| .github/workflows/adapter-conformance-canary.yml | canary: Canary matrix |
| .github/workflows/adapter-contract-drift.yml | aggregate: Aggregate drift report<br>check: ${{ matrix.adapter }} |
| .github/workflows/airgap-e2e.yml | airgap-e2e: Airgap E2E (Linux, real cosign + gpg + unshare) |
| .github/workflows/auto-heal.yml | heal: Apply chosen strategy<br>triage: Triage and classify |
| .github/workflows/auto-release.yml | alert-on-stale-release-trigger: Alert on stale release trigger<br>detect-stale-alerts: Detect open auto-release-skipped issues<br>gate: Release gate<br>release: Tag release<br>sweep-stale-alerts-on-success: Close auto-release-skipped issues on green main |
| .github/workflows/bernstein-ci-fix.yml | fallback-issue: Open ci-fix issue (fallback)<br>fix: Auto-heal with Bernstein<br>tier3-shadow: Tier-3 OpenRouter shadow-mode escalation<br>triage: Triage CI failure |
| .github/workflows/bernstein-issues-decompose.yml | decompose: Implement approved issue plan<br>plan: Plan issue decomposition<br>reject-untrusted-issue: Reject untrusted issue decomposition<br>scope_gate: Require approved file scope |
| .github/workflows/bernstein-pr-review.yml | review: Review with Bernstein |
| .github/workflows/bisect-on-red.yml | bisect: Identify culprit PR |
| .github/workflows/branch-protection-audit.yml | audit: Branch protection audit |
| .github/workflows/ci-gate-stub.yml | ci-gate: ${{ needs.classify.outputs.all_ignored == 'true' && 'CI gate' \|\| 'CI gate stub (not applicable)' }}<br>classify: Classify diff against ci.yml paths-ignore |
| .github/workflows/ci-macos-nightly.yml | open-failure-issue: Open / update macOS nightly failure issue<br>test-macos-nightly: Test (macos-latest, Python ${{ matrix.python-version }}) |
| .github/workflows/ci-topology-heal.yml | heal: Regenerate topology report |
| .github/workflows/ci-weekly-digest.yml | digest: Build and publish weekly digest |
| .github/workflows/ci.yml | actionlint: Workflow lint<br>adapter-conformance-windows: Adapter conformance + e2e (windows)<br>adapter-integration: Adapter integration (fake-CLI)<br>adapter-integration-macos: Adapter integration (fake-CLI, macOS)<br>autofix: Auto-fix lint<br>bandit: Bandit (security)<br>beartype: Beartype (type contracts)<br>ci-gate: CI gate<br>close-ci-issues: Close resolved CI issues<br>coverage-report: Coverage report<br>dead-code: Dead code (Vulture)<br>determine-changes: Determine changes<br>diff-coverage: Diff coverage report<br>dist-size: Package size check<br>install-smoke-pipx: Install smoke - pipx (${{ matrix.os }}, Python ${{ matrix.python-version }})<br>install-smoke-uv: Install smoke - uv tool (${{ matrix.os }})<br>integration-tests: Integration tests<br>lineage-gate: Lineage Gate<br>lint: Lint<br>mutmut-diff: Mutation report (diff-only)<br>mypy-strict-zone: mypy strict (lineage substrate)<br>pip-audit: pip-audit (deps)<br>property-tests: Property tests (Hypothesis smoke)<br>pyright-strict-zone: Pyright strict (security + cluster)<br>repo-hygiene: Repo hygiene<br>schemathesis-smoke: Schemathesis smoke<br>semgrep: Semgrep (custom rules)<br>snapshot-tests: Snapshot tests (syrupy)<br>spelling: Spelling (typos)<br>test: Test (${{ matrix.os }}, Python ${{ matrix.python-version }}, shard ${{ matrix.shard }})<br>test-macos: Test (macos-latest, Python 3.13)<br>typecheck: Type check report |
| .github/workflows/cifuzz-pr.yml | cifuzz: Build and run fuzzers |
| .github/workflows/cleanup-runs.yml | cleanup |
| .github/workflows/cluster-e2e.yml | cluster-e2e: cluster-e2e (linux) |
| .github/workflows/cluster-tunnel-e2e.yml | cluster-tunnel-e2e: cluster-tunnel-e2e (linux) |
| .github/workflows/code-review-bots-ci.yml | sourcery-cli: Sourcery CLI review (advisory) |
| .github/workflows/codeql.yml | analyze: CodeQL (${{ matrix.language }}) |
| .github/workflows/contract-drift-autofix.yml | autofix: Detect and patch contract drift |
| .github/workflows/coverage-ratchet-weekly.yml | bump: Bump diff-coverage floor and open review PR |
| .github/workflows/coverage-ratchet.yml | ratchet: Total coverage ratchet |
| .github/workflows/dependabot-auto-merge.yml | auto-merge |
| .github/workflows/dependency-review.yml | review: Dependency review |
| .github/workflows/docs-drift.yml | drift-check: Run drift check |
| .github/workflows/docs-observability-snapshot.yml | snapshot: Capture snapshot |
| .github/workflows/eval-nightly.yml | bench: bench (full)<br>preflight: preflight (gate)<br>smoke: smoke (synthetic) |
| .github/workflows/hotfix-r-tracker.yml | track: Detect hotfix-begets-hotfix |
| .github/workflows/license-compliance.yml | license-check |
| .github/workflows/main-sha-marker.yml | marker: Main SHA marker |
| .github/workflows/mutation-fixed.yml | mutate: ${{ matrix.module }}<br>summary: Summary + PR comment |
| .github/workflows/nightly-canary.yml | canary: Real-run canary |
| .github/workflows/nightly-deep-tests.yml | bandit-medium-and-high: Bandit (full -ll, advisory)<br>crosshair-pure-fns: CrossHair (concolic, deep)<br>hypothesis-deep: Hypothesis (deep, 1000 examples)<br>mutmut-full: Mutation (full repo, advisory)<br>pip-audit-deep: pip-audit (full closure)<br>schemathesis-deep: Schemathesis (deep, full sweep)<br>stress-leak-suite: Stress + resource-leak suite (TC-C)<br>unit-python-314: Unit tests (Python 3.14, shard ${{ matrix.shard }}) |
| .github/workflows/nightly-drift-sweep.yml | sweep: Open drift-sweep PR if mirrors drifted |
| .github/workflows/pentest.yml | pentest: Pen-test: ${{ github.event.inputs.suite \|\| 'all' }} |
| .github/workflows/post-ci-dispatcher.yml | auto-heal: Auto-heal v2<br>auto-release: Auto-release<br>bernstein-ci-fix: Bernstein CI fix<br>bisect-on-red: Bisect on red<br>meta: Resolve upstream metadata |
| .github/workflows/pr-labels.yml | label |
| .github/workflows/pr-observability-summary.yml | summary: Sticky observability comment |
| .github/workflows/pr-policy.yml | pr-policy: PR policy |
| .github/workflows/publish-docker.yml | publish: Build and push image to GHCR |
| .github/workflows/publish-extension.yml | publish |
| .github/workflows/publish-homebrew.yml | update-formula: Update Homebrew formula |
| .github/workflows/publish.yml | build: Build<br>github-release: Create GitHub Release<br>protocol-gate: Protocol Compatibility Gate<br>publish: Publish to PyPI<br>publish-mcp-registry: Publish MCP registry listing<br>publish-npm: Publish npm wrapper<br>test: Verify tests pass<br>version-check: Verify tag matches pyproject.toml |
| .github/workflows/reconcile-release.yml | reconcile: Compare pyproject.toml vs PyPI |
| .github/workflows/release-major-minor.yml | release: ${{ inputs.bump }} release |
| .github/workflows/required-check-canary.yml | verify: Required-check name canary |
| .github/workflows/review-bot-ack.yml | merge-group-verify: review-bot-ack-queue-verify<br>pr-gate: review-bot-ack-runner |
| .github/workflows/review-bot-sweep.yml | sweep: Sweep recently merged PRs for unprocessed bot findings |
| .github/workflows/sbom.yml | sbom: Generate SBOM |
| .github/workflows/scorecard.yml | analysis: Scorecard analysis<br>upload: Filter suppressions and upload to Code Scanning |
| .github/workflows/soc2-evidence-nightly.yml | pack: generate evidence pack<br>preflight: preflight (gate) |
| .github/workflows/spiffe-extra-e2e.yml | spiffe-extra-e2e: SPIFFE extra E2E (built wheel, extra-present + no-extra suites) |
| .github/workflows/stale.yml | stale |
| .github/workflows/static-analysis-extended.yml | perflint: perflint (hot-path antipatterns)<br>refurb: refurb (idioms)<br>semgrep: Semgrep (CE rules)<br>trivy-fs: Trivy (filesystem)<br>trivy-iac: Trivy (IaC)<br>vulture: vulture (dead code) |
| .github/workflows/trufflehog.yml | trufflehog: trufflehog scan |
| .github/workflows/trunk-health-slo.yml | compute: Compute trunk red-rate and toggle TRUNK_UNSTABLE |
| .github/workflows/typecheck-ts.yml | typecheck: typecheck (${{ matrix.package }}) |
| .github/workflows/zizmor.yml | zizmor: zizmor static analysis |

## Permissions And Secrets

| Workflow | Permissions | Secrets |
| --- | --- | --- |
| .github/workflows/a2a-federation-e2e.yml | workflow: {"contents": "read"} | - |
| .github/workflows/adapter-conformance-canary.yml | workflow: {"contents": "read"}<br>canary: {"contents": "write", "issues": "write", "pull-requests": "write"} | BERNSTEIN_AUTOSYNC_TOKEN, GITHUB_TOKEN |
| .github/workflows/adapter-contract-drift.yml | workflow: {"contents": "read"}<br>aggregate: {"contents": "read", "issues": "write"}<br>check: {"contents": "read"} | ADAPTER_CONTRACT_ANTHROPIC_API_KEY, ADAPTER_CONTRACT_GEMINI_API_KEY, ADAPTER_CONTRACT_OPENAI_API_KEY, GITHUB_TOKEN |
| .github/workflows/airgap-e2e.yml | workflow: {"contents": "read"} | - |
| .github/workflows/auto-heal.yml | heal: {"attestations": "write", "contents": "write", "id-token": "write", "pull-requests": "write"}<br>triage: {"actions": "read", "contents": "read", "pull-requests": "read"} | BERNSTEIN_AUTOSYNC_TOKEN, GITHUB_TOKEN |
| .github/workflows/auto-release.yml | alert-on-stale-release-trigger: {"contents": "read", "issues": "write"}<br>detect-stale-alerts: {"contents": "read", "issues": "read"}<br>gate: {"contents": "read"}<br>release: {"actions": "write", "contents": "write"}<br>sweep-stale-alerts-on-success: {"contents": "read", "issues": "write"} | GITHUB_TOKEN |
| .github/workflows/bernstein-ci-fix.yml | fallback-issue: {"contents": "read", "issues": "write"}<br>fix: {"contents": "write", "issues": "write", "pull-requests": "write"}<br>tier3-shadow: {"actions": "read", "contents": "read"}<br>triage: {"actions": "read", "contents": "read", "pull-requests": "read"} | BERNSTEIN_AUTOSYNC_TOKEN, GEMINI_API_KEY, GITHUB_TOKEN, OPENROUTER_API_KEY_FREE |
| .github/workflows/bernstein-issues-decompose.yml | workflow: {"contents": "read"}<br>decompose: {"contents": "write", "issues": "write", "pull-requests": "write"}<br>plan: {"contents": "read"}<br>reject-untrusted-issue: {"issues": "write"}<br>scope_gate: {"issues": "write"} | ANTHROPIC_API_KEY, BERNSTEIN_AUTOSYNC_TOKEN, GOOGLE_API_KEY, OPENAI_API_KEY |
| .github/workflows/bernstein-pr-review.yml | workflow: {"contents": "read", "pull-requests": "write"} | ANTHROPIC_API_KEY |
| .github/workflows/bisect-on-red.yml | bisect: {"contents": "read", "issues": "write", "pull-requests": "write"} | - |
| .github/workflows/branch-protection-audit.yml | audit: {"contents": "read"} | BRANCH_PROTECTION_AUDIT_TOKEN |
| .github/workflows/ci-gate-stub.yml | workflow: {"contents": "read"}<br>ci-gate: {"contents": "read"}<br>classify: {"contents": "read", "pull-requests": "read"} | - |
| .github/workflows/ci-macos-nightly.yml | workflow: {"contents": "read"}<br>open-failure-issue: {"contents": "read", "issues": "write"}<br>test-macos-nightly: {"checks": "write", "contents": "read"} | GITHUB_TOKEN |
| .github/workflows/ci-topology-heal.yml | workflow: {"contents": "read"}<br>heal: {"contents": "write", "pull-requests": "write"} | BERNSTEIN_AUTOSYNC_TOKEN, GITHUB_TOKEN |
| .github/workflows/ci-weekly-digest.yml | digest: {"actions": "read", "contents": "read", "issues": "write"} | - |
| .github/workflows/ci.yml | workflow: {"contents": "read"}<br>actionlint: {"contents": "read"}<br>adapter-conformance-windows: {"contents": "read"}<br>adapter-integration: {"contents": "read"}<br>adapter-integration-macos: {"contents": "read"}<br>autofix: {"contents": "write"}<br>bandit: {"contents": "read"}<br>beartype: {"contents": "read"}<br>ci-gate: {"contents": "read"}<br>close-ci-issues: {"contents": "read", "issues": "write"}<br>coverage-report: {"contents": "read"}<br>dead-code: {"contents": "read"}<br>determine-changes: {"contents": "read"}<br>diff-coverage: {"contents": "read"}<br>dist-size: {"contents": "read"}<br>install-smoke-pipx: {"contents": "read"}<br>install-smoke-uv: {"contents": "read"}<br>integration-tests: {"contents": "read"}<br>lineage-gate: {"contents": "read"}<br>lint: {"contents": "read"}<br>mutmut-diff: {"contents": "read"}<br>mypy-strict-zone: {"contents": "read"}<br>pip-audit: {"contents": "read"}<br>property-tests: {"contents": "read"}<br>pyright-strict-zone: {"contents": "read"}<br>repo-hygiene: {"contents": "read"}<br>schemathesis-smoke: {"contents": "read"}<br>semgrep: {"contents": "read"}<br>snapshot-tests: {"contents": "read"}<br>spelling: {"contents": "read"}<br>test: {"contents": "read"}<br>test-macos: {"contents": "read"}<br>typecheck: {"contents": "read"} | CODECOV_TOKEN, GITHUB_TOKEN |
| .github/workflows/cifuzz-pr.yml | workflow: {"contents": "read"}<br>cifuzz: {"actions": "read", "contents": "read"} | GITHUB_TOKEN |
| .github/workflows/cleanup-runs.yml | workflow: {"contents": "read"}<br>cleanup: {"actions": "write"} | GITHUB_TOKEN |
| .github/workflows/cluster-e2e.yml | workflow: {"contents": "read"} | - |
| .github/workflows/cluster-tunnel-e2e.yml | workflow: {"contents": "read"} | CF_TUNNEL_HOSTNAME, CF_TUNNEL_TOKEN |
| .github/workflows/code-review-bots-ci.yml | workflow: {"contents": "read"}<br>sourcery-cli: {"contents": "read"} | SOURCERY_API_KEY |
| .github/workflows/codeql.yml | workflow: {"contents": "read"}<br>analyze: {"actions": "read", "contents": "read", "pull-requests": "write", "security-events": "write"} | - |
| .github/workflows/contract-drift-autofix.yml | workflow: {"contents": "write", "issues": "write", "pull-requests": "write"}<br>autofix: {"contents": "write", "issues": "write", "pull-requests": "write"} | BOT_PAT, GITHUB_TOKEN |
| .github/workflows/coverage-ratchet-weekly.yml | bump: {"contents": "write", "pull-requests": "write"} | BERNSTEIN_AUTOSYNC_TOKEN, GITHUB_TOKEN |
| .github/workflows/coverage-ratchet.yml | ratchet: {"actions": "read", "contents": "write", "pull-requests": "write"} | BERNSTEIN_AUTOSYNC_TOKEN, GITHUB_TOKEN |
| .github/workflows/dependabot-auto-merge.yml | workflow: {"contents": "read"}<br>auto-merge: {"contents": "write", "pull-requests": "write"} | GITHUB_TOKEN |
| .github/workflows/dependency-review.yml | workflow: {"contents": "read"}<br>review: {"contents": "read", "pull-requests": "write"} | - |
| .github/workflows/docs-drift.yml | workflow: {"contents": "read"}<br>drift-check: {"contents": "read", "issues": "write", "pull-requests": "write"} | - |
| .github/workflows/docs-observability-snapshot.yml | workflow: {"contents": "read"}<br>snapshot: {"contents": "write", "pull-requests": "write", "security-events": "read"} | BERNSTEIN_AUTOSYNC_TOKEN, GITHUB_TOKEN |
| .github/workflows/eval-nightly.yml | workflow: {"contents": "read"} | EVAL_ENABLED |
| .github/workflows/hotfix-r-tracker.yml | track: {"contents": "read", "issues": "write", "pull-requests": "write"} | - |
| .github/workflows/license-compliance.yml | workflow: {"contents": "read"}<br>license-check: {"contents": "read"} | - |
| .github/workflows/main-sha-marker.yml | - | - |
| .github/workflows/mutation-fixed.yml | workflow: {"contents": "read"}<br>mutate: {"contents": "read"}<br>summary: {"contents": "read", "pull-requests": "write"} | - |
| .github/workflows/nightly-canary.yml | workflow: {"contents": "read"} | - |
| .github/workflows/nightly-deep-tests.yml | workflow: {"contents": "read"}<br>bandit-medium-and-high: {"contents": "read"}<br>crosshair-pure-fns: {"contents": "read"}<br>hypothesis-deep: {"contents": "read"}<br>mutmut-full: {"contents": "read"}<br>pip-audit-deep: {"contents": "read"}<br>schemathesis-deep: {"contents": "read"}<br>stress-leak-suite: {"contents": "read"}<br>unit-python-314: {"contents": "read"} | - |
| .github/workflows/nightly-drift-sweep.yml | workflow: {"contents": "read"}<br>sweep: {"contents": "write", "pull-requests": "write"} | BERNSTEIN_AUTOSYNC_TOKEN, GITHUB_TOKEN |
| .github/workflows/pentest.yml | workflow: {"contents": "read"} | - |
| .github/workflows/post-ci-dispatcher.yml | auto-heal: {"actions": "read", "attestations": "write", "contents": "write", "id-token": "write", "pull-requests": "write"}<br>auto-release: {"actions": "write", "contents": "write", "issues": "write"}<br>bernstein-ci-fix: {"actions": "read", "contents": "write", "issues": "write", "pull-requests": "write"}<br>bisect-on-red: {"contents": "read", "issues": "write", "pull-requests": "write"}<br>meta: {"contents": "read"} | BERNSTEIN_AUTOSYNC_TOKEN, GEMINI_API_KEY, OPENROUTER_API_KEY_FREE |
| .github/workflows/pr-labels.yml | workflow: {"contents": "read"}<br>label: {"contents": "read", "issues": "write", "pull-requests": "write"} | GITHUB_TOKEN |
| .github/workflows/pr-observability-summary.yml | workflow: {"contents": "read"}<br>summary: {"checks": "read", "contents": "read", "pull-requests": "write", "security-events": "read"} | GITHUB_TOKEN |
| .github/workflows/pr-policy.yml | workflow: {"contents": "read"}<br>pr-policy: {"actions": "read", "contents": "write", "pull-requests": "read"} | BERNSTEIN_AUTOSYNC_TOKEN |
| .github/workflows/publish-docker.yml | publish: {"attestations": "write", "contents": "read", "id-token": "write", "packages": "write"} | GITHUB_TOKEN |
| .github/workflows/publish-extension.yml | workflow: {"contents": "read"}<br>publish: {"contents": "write"} | OPEN_VSX_TOKEN, VS_MARKETPLACE_TOKEN |
| .github/workflows/publish-homebrew.yml | workflow: {"contents": "read"}<br>update-formula: {"contents": "read"} | HOMEBREW_TAP_TOKEN |
| .github/workflows/publish.yml | build: {"contents": "read"}<br>github-release: {"actions": "write", "contents": "write"}<br>protocol-gate: {"contents": "read"}<br>publish: {"attestations": "write", "contents": "read", "id-token": "write"}<br>publish-mcp-registry: {"contents": "read", "id-token": "write"}<br>publish-npm: {"contents": "read"}<br>test: {"contents": "read"}<br>version-check: {"contents": "read"} | GITHUB_TOKEN, NPM_TOKEN |
| .github/workflows/reconcile-release.yml | reconcile: {"contents": "read", "issues": "write"} | - |
| .github/workflows/release-major-minor.yml | workflow: {"contents": "read"}<br>release: {"attestations": "write", "contents": "write", "id-token": "write"} | GITHUB_TOKEN |
| .github/workflows/required-check-canary.yml | verify: {"contents": "read"} | - |
| .github/workflows/review-bot-ack.yml | merge-group-verify: {"checks": "write", "contents": "read", "pull-requests": "read"}<br>pr-gate: {"checks": "write", "contents": "read", "issues": "write", "pull-requests": "write"} | - |
| .github/workflows/review-bot-sweep.yml | sweep: {"contents": "write", "pull-requests": "write"} | GITHUB_TOKEN, LANDING_REPO_PAT |
| .github/workflows/sbom.yml | workflow: {"contents": "read"}<br>sbom: {"contents": "write"} | - |
| .github/workflows/scorecard.yml | workflow: {"contents": "read"}<br>analysis: {"actions": "read", "contents": "read", "id-token": "write", "security-events": "write"}<br>upload: {"contents": "read", "security-events": "write"} | - |
| .github/workflows/soc2-evidence-nightly.yml | workflow: {"contents": "read"} | SOC2_EVIDENCE_ENABLED |
| .github/workflows/spiffe-extra-e2e.yml | workflow: {"contents": "read"} | - |
| .github/workflows/stale.yml | workflow: {"issues": "write", "pull-requests": "write"} | - |
| .github/workflows/static-analysis-extended.yml | workflow: {"contents": "read"}<br>perflint: {"contents": "read", "security-events": "write"}<br>refurb: {"contents": "read", "security-events": "write"}<br>semgrep: {"contents": "read", "security-events": "write"}<br>trivy-fs: {"contents": "read", "security-events": "write"}<br>trivy-iac: {"contents": "read", "security-events": "write"}<br>vulture: {"contents": "read", "security-events": "write"} | - |
| .github/workflows/trufflehog.yml | workflow: {"contents": "read"}<br>trufflehog: {"contents": "read", "pull-requests": "read"} | - |
| .github/workflows/trunk-health-slo.yml | compute: {"actions": "read"} | BOT_PAT |
| .github/workflows/typecheck-ts.yml | workflow: {"contents": "read"}<br>typecheck: {"contents": "read"} | - |
| .github/workflows/zizmor.yml | workflow: {"contents": "read"}<br>zizmor: {"actions": "read", "contents": "read", "security-events": "write"} | - |

## Cross-Workflow Calls

| Caller workflow | Reusable workflow calls |
| --- | --- |
| .github/workflows/post-ci-dispatcher.yml | auto-heal -> ./.github/workflows/auto-heal.yml (needs: meta)<br>auto-release -> ./.github/workflows/auto-release.yml (needs: meta)<br>bernstein-ci-fix -> ./.github/workflows/bernstein-ci-fix.yml (needs: ["meta", "auto-heal"])<br>bisect-on-red -> ./.github/workflows/bisect-on-red.yml (needs: meta) |

## Artifact Hand-Offs

| Workflow | Artifact steps |
| --- | --- |
| .github/workflows/adapter-conformance-canary.yml | canary: upload adapter-canary-receipts |
| .github/workflows/adapter-contract-drift.yml | aggregate: download -<br>check: upload drift-${{ matrix.adapter }} |
| .github/workflows/bernstein-ci-fix.yml | tier3-shadow: upload tier3-shadow-${{ needs.triage.outputs.short_sha }} |
| .github/workflows/bernstein-issues-decompose.yml | decompose: download issue-decompose-plan-${{ github.event.issue.number }}<br>plan: upload issue-decompose-plan-${{ github.event.issue.number }} |
| .github/workflows/ci.yml | coverage-report: download -<br>coverage-report: upload coverage-report<br>diff-coverage: download coverage-report<br>dist-size: upload install-smoke-wheel<br>install-smoke-pipx: download install-smoke-wheel<br>install-smoke-uv: download install-smoke-wheel<br>test: upload coverage-data-${{ matrix.shard }} |
| .github/workflows/cifuzz-pr.yml | cifuzz: upload cifuzz-artifacts-address |
| .github/workflows/cluster-e2e.yml | cluster-e2e: upload cluster-e2e-logs |
| .github/workflows/cluster-tunnel-e2e.yml | cluster-tunnel-e2e: upload cluster-tunnel-e2e-logs |
| .github/workflows/coverage-ratchet.yml | ratchet: download coverage-report |
| .github/workflows/docs-drift.yml | drift-check: upload docs-drift-report |
| .github/workflows/eval-nightly.yml | bench: upload eval-nightly-${{ github.run_id }}<br>smoke: upload eval-nightly-smoke |
| .github/workflows/license-compliance.yml | license-check: upload license-report |
| .github/workflows/mutation-fixed.yml | mutate: upload mutation-${{ matrix.module }}<br>summary: download - |
| .github/workflows/nightly-deep-tests.yml | bandit-medium-and-high: upload nightly-bandit-results<br>mutmut-full: upload nightly-mutmut-results |
| .github/workflows/pentest.yml | pentest: upload pentest-results-${{ github.run_number }} |
| .github/workflows/publish.yml | build: upload dist<br>github-release: download dist<br>publish: download dist |
| .github/workflows/sbom.yml | sbom: upload sbom |
| .github/workflows/scorecard.yml | analysis: upload scorecard-results<br>upload: download scorecard-results |
| .github/workflows/soc2-evidence-nightly.yml | pack: upload soc2-evidence-${{ github.run_id }} |
| .github/workflows/static-analysis-extended.yml | perflint: upload perflint-sarif<br>refurb: upload refurb-sarif<br>semgrep: upload semgrep-sarif<br>trivy-fs: upload trivy-fs-sarif<br>trivy-iac: upload trivy-iac-sarif<br>vulture: upload vulture-sarif |
