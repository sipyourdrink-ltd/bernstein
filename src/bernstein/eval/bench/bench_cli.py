"""
bernstein-bench: CLI entry points (Click).

Registered in src/bernstein/cli/main.py alongside every other subcommand:

    from bernstein.eval.bench.bench_cli import bench_group
    cli.add_command(bench_group)

This exposes:
    bernstein bench run <suite> [--out <path>] [--scheduler <name>] [--stub-signer]
                        [--reliability K] [--budget <usd>] [--ci] [--sarif-out <path>]
                        [--baseline <path>] [--regression-threshold <float>]
                        [--repo <slug>] [--head-sha <sha>]
    bernstein bench compare <a> <b> [--allow-harness-drift] [--format text|markdown|json]
    bernstein bench verify <bundle> [--suite <name>]
    bernstein bench reliability-verify <receipt> [--suite <name>]
    bernstein bench reliability-check <receipt> [--suite <name>] [--task <id>] [--attempt N]

Also registered as a standalone script in pyproject.toml:
    bernstein-bench = "bernstein.eval.bench.bench_cli:bench_group"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from bernstein.eval.bench.bundle import SubmissionBundle
    from bernstein.eval.bench.runner import ReplayAdapter
    from bernstein.eval.bench.suite import BenchSuite

# ---------------------------------------------------------------------------
# Suite registry
# ---------------------------------------------------------------------------


def _get_suite(name: str):
    """Resolve a suite name or .json path to a BenchSuite."""
    from bernstein.eval.bench.gate_evasion_suite import build_gate_evasion_suite_v1
    from bernstein.eval.bench.goal_drift_suite import build_goal_drift_suite
    from bernstein.eval.bench.golden_suite import build_golden_suite_v1
    from bernstein.eval.bench.leakage_suite import build_leakage_suite_v1
    from bernstein.eval.bench.suite import BenchSuite
    from bernstein.eval.bench.tool_surface_suite import build_tool_surface_suite

    _BUILTIN = {
        "gate-evasion-v1": build_gate_evasion_suite_v1,
        "goal-drift-v1": build_goal_drift_suite,
        "golden-v1": build_golden_suite_v1,
        "tool-surface-v1": build_tool_surface_suite,
        "leakage-v1": build_leakage_suite_v1,
    }

    if name in _BUILTIN:
        return _BUILTIN[name]()

    path = Path(name)
    if path.suffix == ".json" and path.exists():
        return BenchSuite.load(path)

    raise click.BadParameter(
        f"Unknown suite {name!r}. Built-in suites: {', '.join(_BUILTIN)}. Or pass a path to a .json suite file.",
        param_hint="suite",
    )


def _resolve_adapter(suite_obj: BenchSuite) -> ReplayAdapter:
    """The adapter that scores *suite_obj*: the suite's own, else the synthetic mock.

    Only ``tool-surface-v1``, ``gate-evasion-v1``, and ``goal-drift-v1`` have an adapter
    that derives a verdict from a run. Every other suite (``golden-v1`` and any ``.json``
    suite) falls back to ``MockReplayAdapter``, which passes everything; callers must check
    :func:`_is_synthetic`.
    """
    from bernstein.eval.bench.runner import MockReplayAdapter

    if suite_obj.version == "tool-surface-v1":
        from bernstein.eval.bench.tool_surface_suite import ToolSurfaceReplayAdapter

        return ToolSurfaceReplayAdapter()
    if suite_obj.version == "gate-evasion-v1":
        from bernstein.eval.bench.gate_evasion_suite import GateEvasionReplayAdapter

        return GateEvasionReplayAdapter()
    if suite_obj.version == "goal-drift-v1":
        from bernstein.eval.bench.goal_drift_suite import GoalDriftReplayAdapter

        return GoalDriftReplayAdapter()

    if suite_obj.version == "leakage-v1":
        from bernstein.eval.bench.leakage_suite import LeakageReplayAdapter

        return LeakageReplayAdapter()
    return MockReplayAdapter()


def _is_synthetic(adapter: object) -> bool:
    """Whether *adapter* reports verdicts that no run produced."""
    return bool(getattr(adapter, "synthetic", False))


_MOCK_NOTICE = (
    "MOCK adapter: this suite has no production adapter, so every verdict is synthetic "
    "(pass, score 1.0) and was not derived from running any task."
)


def _refuse_install_identity_for_mock(suite_obj: BenchSuite, what: str) -> None:
    """Fail closed rather than sign synthetic verdicts with the real install identity."""
    raise click.ClickException(
        f"Suite {suite_obj.version!r} has no production adapter, so its verdicts would come from the "
        f"synthetic mock adapter (every task passes, score 1.0). Refusing to sign {what} with the "
        "install identity: the signature would attest a result nothing produced. "
        "Pass --stub-signer to produce a test-grade, mock-labelled output."
    )


#: The checkout this package lives in:
#: ``<root>/src/bernstein/eval/bench/bench_cli.py`` -> ``<root>``.
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _repo_relative(path: Path) -> str | None:
    """*path* as a repository-relative posix path, or ``None`` if it is outside.

    A SARIF location is read against the repository the report is attached
    to, so an absolute path from the runner anchors nothing there -- and it
    publishes the layout of the machine that produced it into an artefact
    meant for a code-scanning UI. Outside the checkout there is no honest
    answer, and no location is the honest one.
    """
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return None


def _suite_source_uri(name: str) -> str | None:
    """Repository-relative path the suite's tasks are defined in, for SARIF.

    A .json suite is its own file; a built-in suite lives in the module that
    builds it. Returns ``None`` when neither can be named *relative to this
    checkout*, so the report carries no location rather than an invented or
    machine-local one.
    """
    import inspect

    from bernstein.eval.bench import (
        gate_evasion_suite,
        goal_drift_suite,
        golden_suite,
        leakage_suite,
        tool_surface_suite,
    )

    path = Path(name)
    if path.suffix == ".json" and path.exists():
        return _repo_relative(path)
    module = {
        "golden-v1": golden_suite,
        "tool-surface-v1": tool_surface_suite,
        "gate-evasion-v1": gate_evasion_suite,
        "goal-drift-v1": goal_drift_suite,
        "leakage-v1": leakage_suite,
    }.get(name)
    if module is None:
        return None
    source = inspect.getsourcefile(module)
    if source is None:
        return None
    source_path = Path(source).resolve()
    # Name the file relative to the checkout this package lives in, so a
    # `bernstein` directory elsewhere in the path cannot mislead the slice.
    relative = _repo_relative(source_path)
    if relative is not None:
        return relative
    parts = source_path.parts
    # .../src/bernstein/eval/bench/<module>.py -> src/bernstein/eval/bench/<module>.py
    try:
        return Path(*parts[parts.index("bernstein") - 1 :]).as_posix()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Top-level group: bernstein bench
# ---------------------------------------------------------------------------


@click.group(name="bench")
def bench_group() -> None:
    """Runnable, reproducibility-gated evaluation harness.

    \b
    bernstein bench run golden-v1 --out bundle.json
    bernstein bench compare bundle_a.json bundle_b.json
    bernstein bench verify bundle.json
    """


# ---------------------------------------------------------------------------
# bernstein bench run
# ---------------------------------------------------------------------------


@bench_group.command(name="run")
@click.argument("suite")
@click.option("--out", default="bundle.json", show_default=True, help="Output path for the submission bundle.")
@click.option("--scheduler", default="default", show_default=True, help="Scheduler name to embed in the bundle.")
@click.option(
    "--stub-signer",
    is_flag=True,
    default=False,
    help="Use the stub signer instead of the install identity (for testing).",
)
@click.option(
    "--reliability",
    "reliability_k",
    type=click.IntRange(min=1),
    default=None,
    metavar="K",
    help=(
        "Run each task K times under fixed coordination and emit a signed "
        "pass^k reliability receipt instead of a submission bundle."
    ),
)
@click.option(
    "--budget",
    type=float,
    default=None,
    help="Stop running tasks once cumulative cost reaches this USD limit. Not combined with --reliability.",
)
@click.option(
    "--trajectory",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to trajectory JSON file (events and diff).",
)
@click.option(
    "--diff",
    "diff_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to git diff file for goal-drift suite.",
)
@click.option(
    "--threshold",
    type=float,
    default=0.0,
    show_default=True,
    help="Drift threshold tolerance for goal-drift suite.",
)
@click.option(
    "--require-step-coverage",
    is_flag=True,
    default=False,
    help="Enforce 100% step coverage as a strict gate for goal-drift suite.",
)
@click.option(
    "--smoke-synthetic",
    is_flag=True,
    default=False,
    help=(
        "Synthesize compliant mock trajectories for plumbing/smoke testing "
        "(labels adapter synthetic; requires --stub-signer)."
    ),
)
@click.option(
    "--ci",
    is_flag=True,
    default=False,
    help="Run in CI mode: output SARIF, evaluate scorecard against baseline, and check regression.",
)
@click.option(
    "--sarif-out",
    default=None,
    help="Output file path for SARIF v2.1.0 diagnostic report.",
)
@click.option(
    "--baseline",
    default=None,
    help="Path to baseline submission bundle .json for delta scorecard comparison.",
)
@click.option(
    "--regression-threshold",
    # A negative tolerance would invert the gate: a perfect run "regresses".
    type=click.FloatRange(min=0.0),
    default=0.0,
    show_default=True,
    help="Allowed pass rate drop before CI fails (e.g. 0.05 for 5% tolerance).",
)
@click.option(
    "--repo",
    default="",
    help="GitHub repository slug (owner/repo) to publish check run to.",
)
@click.option(
    "--head-sha",
    default="",
    help="Commit SHA to attach check run to.",
)
def bench_run(
    suite: str,
    out: str,
    scheduler: str,
    stub_signer: bool,
    reliability_k: int | None,
    budget: float | None = None,
    trajectory: Path | None = None,
    diff_file: Path | None = None,
    threshold: float = 0.0,
    require_step_coverage: bool = False,
    smoke_synthetic: bool = False,
    ci: bool = False,
    sarif_out: str | None = None,
    baseline: str | None = None,
    regression_threshold: float = 0.0,
    repo: str = "",
    head_sha: str = "",
) -> None:
    """Execute a suite and emit a signed submission bundle.

    SUITE is a built-in suite name (e.g. golden-v1) or a path to a .json
    suite file.

    With --reliability K, every task is run K times with the scheduler
    config held byte-identical across attempts, and the output is a signed
    reliability receipt reporting pass@1 (any attempt passed) and pass^k
    (all K attempts passed — the headline floor).
    """
    from bernstein.eval.bench.bundle import SubmissionBundle
    from bernstein.eval.bench.runner import BenchRunner
    from bernstein.eval.bench.signer import AgentCardSigner, StubSigner

    # Every CI output -- the SARIF report, the scorecard, the check run --
    # is computed from a submission bundle, and --reliability emits a
    # reliability receipt instead of one. Accepting the flags and returning
    # before they do anything is the failure mode the budget gate was told
    # not to have: a cap that is accepted and not applied.
    if reliability_k is not None:
        unavailable = [
            flag
            for flag, given in (
                ("--ci", ci),
                ("--sarif-out", bool(sarif_out)),
                ("--baseline", bool(baseline)),
                ("--repo", bool(repo)),
                ("--head-sha", bool(head_sha)),
            )
            if given
        ]
        if unavailable:
            raise click.ClickException(
                f"{', '.join(unavailable)} cannot be combined with --reliability: that path emits a "
                "reliability receipt, and the SARIF report, scorecard and check run are all computed "
                "from a submission bundle. Run without --reliability to produce them."
            )

    if baseline and not Path(baseline).is_file():
        raise click.ClickException(f"Baseline bundle not found: {baseline}")

    # A baseline, a repo or a head SHA is a request for the comparison it
    # feeds. Accepting one and producing nothing left an operator reading a
    # zero exit as "no regression" when nothing had been compared.
    ci_outputs = bool(ci or sarif_out or baseline or repo or head_sha)

    suite_obj = _get_suite(suite)
    click.echo(f"Suite       : {suite_obj.version}")
    click.echo(f"Suite hash  : {suite_obj.suite_hash}")
    click.echo(f"Tasks       : {len(suite_obj.tasks)}")

    if reliability_k is not None:
        # The reliability runner does not enforce a budget. A cap that is
        # accepted and not applied is worse than one that is refused.
        if budget is not None:
            raise click.ClickException(
                "--budget is not enforced on the --reliability path. "
                "Run without --reliability to cap spend, or without --budget to measure pass^k."
            )
        _run_reliability(suite_obj, scheduler, reliability_k, Path(out), stub_signer)
        return

    adapter: ReplayAdapter
    if suite_obj.version == "goal-drift-v1":
        from bernstein.eval.bench.goal_drift_suite import GoalDriftReplayAdapter

        trajectory_data = None
        if trajectory is not None:
            with open(trajectory, encoding="utf-8") as f:
                trajectory_data = json.load(f)
        diff_text = None
        if diff_file is not None:
            with open(diff_file, encoding="utf-8") as f:
                diff_text = f.read()

        adapter = GoalDriftReplayAdapter(
            trajectory_data=trajectory_data,
            diff_text=diff_text,
            threshold=threshold,
            smoke_synthetic=smoke_synthetic,
            require_step_coverage=require_step_coverage,
        )
    else:
        adapter = _resolve_adapter(suite_obj)

    scheduler_config: dict[str, Any] = {"scheduler": scheduler, "threshold": threshold}
    synthetic = _is_synthetic(adapter)
    if synthetic:
        if not stub_signer:
            _refuse_install_identity_for_mock(suite_obj, "this bundle")
        # Part of scheduler_config, so it is hashed, signed and part of the harness fingerprint:
        # the bundle says what scored it, and cannot be ranked against a really-scored one.
        scheduler_config["adapter"] = "mock"
        click.echo(f"Adapter     : {_MOCK_NOTICE}", err=True)
    runner = BenchRunner(
        suite=suite_obj,
        adapter=adapter,
        scheduler_config=scheduler_config,
        budget_usd=budget,
    )

    click.echo("\nRunning tasks…")
    bundle = runner.run()

    signer = StubSigner() if stub_signer else AgentCardSigner()
    bundle = signer.sign(bundle)

    out_path = Path(out)
    bundle.save(out_path)

    mock_tag = "  (MOCK: synthetic, not a measured result)" if synthetic else ""
    click.echo(f"\nScore       : {bundle.overall_score * 100:.1f}%{mock_tag}")
    click.echo(f"Pass rate   : {bundle.pass_rate * 100:.1f}%")
    if suite_obj.version == "leakage-v1":
        # The pass rate alone cannot tell "scanned and dirty" from "never
        # scanned", and both are inside it. Print what the run did not cover
        # next to the number that would otherwise imply it did.
        from bernstein.eval.bench.leakage_suite import score_from_bundle

        leakage = score_from_bundle(bundle)
        click.echo(f"Canaries    : {leakage.total_canaries_tested} across {leakage.total_scanned_surfaces} surface(s)")
        if leakage.surfaces_not_exercised:
            click.echo(f"Not scanned : {', '.join(leakage.surfaces_not_exercised)} (needs a governed run)")
        if leakage.seed_points_not_exercised:
            click.echo(f"Not seeded  : {', '.join(leakage.seed_points_not_exercised)} (needs a governed run)")
        for collapsed in leakage.encodings_collapsed:
            click.echo(f"Same bytes  : {collapsed}")
    click.echo(f"Total tokens: {bundle.total_tokens:,}")
    click.echo(f"Total cost  : ${bundle.total_cost_usd:.4f}")
    click.echo(f"Bundle hash : {bundle.bundle_hash()}")
    click.echo(f"Signed by   : {bundle.signer_fingerprint or '(unsigned)'}")
    click.echo(f"\nBundle written to: {out_path}")

    if ci_outputs:
        from bernstein.eval.bench.ci import evaluate_ci_scorecard, post_bench_check_run
        from bernstein.eval.bench.sarif import bundle_to_sarif
        from bernstein.eval.bench.verifier import BenchVerifier
        from bernstein.github_app.check_runs import CheckRunClient

        # The report is written for --ci or an explicit --sarif-out; a
        # --baseline on its own asks for the comparison, not for a file.
        if ci or sarif_out:
            sarif_data = bundle_to_sarif(bundle, suite_obj, suite_uri=_suite_source_uri(suite))
            sarif_target = Path(sarif_out) if sarif_out else out_path.with_suffix(".sarif")
            sarif_target.parent.mkdir(parents=True, exist_ok=True)
            sarif_target.write_text(json.dumps(sarif_data, indent=2), encoding="utf-8")
            click.echo(f"SARIF report written to: {sarif_target}")

        # A baseline that was asked for and is not there is a configuration
        # error, not a neutral result. One that is there but does not load
        # (hash mismatch, malformed) is what "unverifiable" means: neutral,
        # with the reason on the scorecard.
        baseline_bundle = None
        baseline_problem: str | None = None
        if baseline:
            base_path = Path(baseline)
            try:
                baseline_bundle = SubmissionBundle.load(base_path)
            except Exception as exc:
                baseline_problem = (
                    f"Baseline bundle {base_path.name} could not be loaded "
                    f"({type(exc).__name__}: {exc}). Result is neutral."
                )

        verifier = BenchVerifier(suite=suite_obj, adapter=adapter, allow_stub_signature=stub_signer)
        scorecard = evaluate_ci_scorecard(
            bundle=bundle,
            suite=suite_obj,
            baseline_bundle=baseline_bundle,
            verifier=verifier,
            regression_threshold=regression_threshold,
        )
        if baseline_problem is not None:
            scorecard.summary = baseline_problem
        click.echo("\n" + scorecard.to_markdown())

        # A check run that was asked for and did not get posted must be
        # announced, not logged at debug: the operator passed --repo and
        # --head-sha to get one, and silence here reads as success.
        if repo and head_sha:
            client = CheckRunClient(repo=repo)
            posted = post_bench_check_run(scorecard=scorecard, client=client, head_sha=head_sha)
            if posted is None:
                click.echo(
                    "Warning: the bench scorecard check run was not posted (GitHub client not "
                    "configured, or the API call failed); the conclusion above reached no check run.",
                    err=True,
                )
        elif repo or head_sha:
            click.echo(
                "Warning: --repo and --head-sha are both needed to post the check run; nothing was posted.",
                err=True,
            )

        if ci and scorecard.conclusion == "failure":
            sys.exit(1)

    # A run the budget cut short is not a completed run. Say so where a CI
    # log reader will see it, and exit non-zero: the bundle still records
    # every refusal receipt, but "score 20%" alone cannot be told apart from
    # "one of five passed" (#5464 review, F4).
    # The same predicate the verifier scores against and the comparison
    # counts. This read ``harness_output["refusal"]``, so a receipt carrying
    # the canonical status and no harness output was refused, verified clean,
    # and was never mentioned here.
    refused = bundle.refused_results()
    if refused:
        click.echo(
            f"\nBudget exceeded: limit ${budget:.4f}, spent ${bundle.total_cost_usd:.4f}; "
            f"{len(refused)}/{len(bundle.task_results)} tasks refused and recorded as refusal receipts."
        )
        sys.exit(2)


# ---------------------------------------------------------------------------
# bernstein bench verify
# ---------------------------------------------------------------------------


@bench_group.command(name="verify")
@click.argument("bundle")
@click.option("--suite", default="golden-v1", show_default=True, help="Suite to verify against.")
@click.option(
    "--trusted-key",
    "trusted_keys",
    multiple=True,
    metavar="FINGERPRINT=PATH",
    help="A signer fingerprint and the SPKI PEM file that verifies it. Repeatable.",
)
@click.option(
    "--stub-signer",
    is_flag=True,
    default=False,
    help="Accept a bundle signed with the PUBLIC stub key. Proves nothing about origin; for testing.",
)
@click.option(
    "--no-signature",
    is_flag=True,
    default=False,
    help="Skip the signature check. Replay still runs; the bundle's origin is then unattested.",
)
def bench_verify(
    bundle: str,
    suite: str,
    trusted_keys: tuple[str, ...],
    stub_signer: bool,
    no_signature: bool,
) -> None:
    """Verify a bundle by replaying every task receipt offline.

    BUNDLE is the path to a submission bundle .json file.

    The signature is checked FIRST, because it is the only part of a bundle a forger cannot
    reproduce: every hash in one can be recomputed by whoever rebuilt it. Supply the signer's
    public key with --trusted-key FINGERPRINT=PATH; without one, an install-identity signature
    cannot be resolved and the bundle is reported UNSIGNED rather than assumed good.

    Exits 0 on MATCH, 1 on any divergence, fabricated score, or unverifiable signature.
    """
    from bernstein.eval.bench.bundle import SubmissionBundle
    from bernstein.eval.bench.verifier import BenchVerifier

    bundle_path = Path(bundle)
    if not bundle_path.exists():
        raise click.ClickException(f"Bundle file not found: {bundle_path}")

    # Loading rebuilds the bundle and recomputes its hash; a file edited after it was written fails
    # here. That is a verdict about the bundle, not a crash, so it is reported as one.
    try:
        bundle_obj = SubmissionBundle.load(bundle_path)
    except (ValueError, KeyError, TypeError) as exc:
        raise click.ClickException(
            f"Bundle {bundle_path} is not a valid, untampered bundle: {type(exc).__name__}: {exc}"
        ) from exc
    suite_obj = _get_suite(suite)

    adapter = _resolve_adapter(suite_obj)
    keys: dict[str, bytes] = {}
    for entry in trusted_keys:
        fingerprint, sep, key_path = entry.partition("=")
        if not sep or not fingerprint or not key_path:
            raise click.ClickException(f"--trusted-key expects FINGERPRINT=PATH, got {entry!r}")
        pem = Path(key_path)
        if not pem.exists():
            raise click.ClickException(f"Trusted key file not found: {pem}")
        keys[fingerprint] = pem.read_bytes()

    verifier = BenchVerifier(
        suite=suite_obj,
        adapter=adapter,
        trusted_keys=keys,
        allow_stub_signature=stub_signer,
        require_signature=not no_signature,
    )
    result = verifier.verify(bundle_obj)

    click.echo(result.report())
    if _is_synthetic(adapter):
        click.echo(
            f"\n{_MOCK_NOTICE} MATCH here covers receipt integrity, task coverage and signature only; "
            "it does not show that any task passed.",
        )
    sys.exit(0 if result.passed else 1)


# ---------------------------------------------------------------------------
# bernstein bench compare
# ---------------------------------------------------------------------------


@bench_group.command(name="compare")
@click.argument("a")
@click.argument("b")
@click.option(
    "--allow-harness-drift",
    is_flag=True,
    default=False,
    help="Rank even when the two bundles' harness fingerprints differ.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "markdown", "json"]),
    default="text",
    show_default=True,
    help="text ranks and prints deltas; markdown and json render the full comparison report.",
)
def bench_compare(a: str, b: str, allow_harness_drift: bool, output_format: str) -> None:
    """Compare two submission bundles, ranking by expected value.

    A and B are paths to submission bundle .json files.

    The expected value is computed as (resolved - lambda * wrong) / attempted,
    where resolved is the number of passed tasks, wrong is the number of tasks
    that did not pass, attempted is the total tasks (a bundle records no
    abstentions), and lambda is the bundle's own signed lambda_value
    (default 0.5).

    Bundles are not verified here: run `bench verify` first.

    The harness fingerprint is recomputed from each bundle's raw
    scheduler_config before it is trusted.  Bundles from different
    harnesses are not ranked against each other unless
    --allow-harness-drift is passed: a score gap across differing
    harness settings is a harness change, not a model change.

    Exits 0 when the bundles are ranked, 1 on harness mismatch without
    the flag or on a fingerprint integrity failure.
    """
    from bernstein.eval.bench.bundle import SubmissionBundle, harness_fingerprint

    path_a, path_b = Path(a), Path(b)
    for path in (path_a, path_b):
        if not path.exists():
            raise click.ClickException(f"Bundle file not found: {path}")

    bundle_a = SubmissionBundle.load(path_a)
    bundle_b = SubmissionBundle.load(path_b)

    # Recompute before trust: a stored fingerprint that disagrees with
    # the settings beside it is an integrity failure, not drift.
    expected_a = harness_fingerprint(bundle_a.scheduler_config)
    expected_b = harness_fingerprint(bundle_b.scheduler_config)
    for path, bundle, expected in ((path_a, bundle_a, expected_a), (path_b, bundle_b, expected_b)):
        if bundle.harness_fingerprint != expected:
            raise click.ClickException(
                f"Harness fingerprint integrity failure: {path} stores "
                f"{bundle.harness_fingerprint[:12]}… but its scheduler_config "
                f"hashes to {expected[:12]}…."
            )

    # With a machine-readable format the report owns stdout; the harness
    # verdict still has to be said, so it goes to stderr there.
    to_stderr = output_format != "text"
    fp_a, fp_b = bundle_a.harness_fingerprint, bundle_b.harness_fingerprint
    if fp_a != fp_b:
        differing = sorted(
            key
            for key in set(bundle_a.scheduler_config) | set(bundle_b.scheduler_config)
            if bundle_a.scheduler_config.get(key) != bundle_b.scheduler_config.get(key)
        )
        click.echo(f"Harness fingerprints differ: {fp_a[:12]}… vs {fp_b[:12]}…", err=to_stderr)
        click.echo(f"Differing harness settings: {', '.join(differing)}", err=to_stderr)
        if not allow_harness_drift:
            click.echo(
                "Refusing to rank: a score gap across differing harness settings "
                "is a harness change, not a model change. "
                "Pass --allow-harness-drift to rank anyway.",
                err=to_stderr,
            )
            sys.exit(1)
        click.echo("--allow-harness-drift: ranking anyway.", err=to_stderr)
    else:
        click.echo(f"Harness fingerprint: {fp_a} (match)", err=to_stderr)

    # The harness check above gates every format: a cost or token delta
    # across differing harness settings is as meaningless as a score delta.
    from bernstein.eval.bench.compare import compare_bundles

    try:
        report = compare_bundles(bundle_a, bundle_b)
    except ValueError as exc:
        if not allow_harness_drift:
            raise
        click.echo(f"--allow-harness-drift: {exc}", err=to_stderr)
        report = compare_bundles(bundle_a, bundle_b, check_fingerprint=False)
    if output_format == "json":
        click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    if output_format == "markdown":
        click.echo(report.to_markdown())
        return

    ordered = sorted([(path_a, bundle_a), (path_b, bundle_b)], key=lambda p: -p[1].expected_value())
    click.echo("")
    for rank, (path, bundle) in enumerate(ordered, start=1):
        click.echo(
            f"{rank}. {path.name}: score {bundle.overall_score * 100:.1f}%, "
            f"pass rate {bundle.pass_rate * 100:.1f}% over {len(bundle.task_results)} tasks, "
            f"expected value {bundle.expected_value():.3f} (lambda {bundle.lambda_value:g})"
        )
    if bundle_a.lambda_value != bundle_b.lambda_value:
        click.echo(
            f"Warning: the bundles carry different lambda values ({bundle_a.lambda_value:g} vs "
            f"{bundle_b.lambda_value:g}); each is ranked by its own, so the expected values are "
            "not on one scale."
        )
    click.echo(
        "Note: bundles are ranked as given. Signatures and receipts are not verified here "
        "(run `bernstein bench verify` on each bundle first)."
    )
    if any(r.has_resource_metrics() for r in (*bundle_a.task_results, *bundle_b.task_results)):
        click.echo("")
        click.echo(
            f"Cost     : ${report.cost_a_usd:.4f} -> ${report.cost_b_usd:.4f} "
            f"({report.cost_delta_usd:+.4f}, {report.cost_delta_percent_text()})"
        )
        click.echo(f"Tokens   : {report.tokens_a:,} -> {report.tokens_b:,} ({report.tokens_delta:+,})")
        click.echo(
            f"Duration : {report.duration_a_seconds:.2f}s -> {report.duration_b_seconds:.2f}s "
            f"({report.duration_delta_seconds:+.2f}s)"
        )
        if report.refused_a or report.refused_b:
            click.echo(f"Refused  : {report.refused_a} -> {report.refused_b} tasks never ran (budget)")
    else:
        _echo_cost_delta(path_a, bundle_a, path_b, bundle_b)
        if report.refused_a or report.refused_b:
            click.echo(f"Refused  : {report.refused_a} -> {report.refused_b} tasks never ran (budget)")


def _echo_cost_delta(
    path_a: Path,
    bundle_a: SubmissionBundle,
    path_b: Path,
    bundle_b: SubmissionBundle,
) -> None:
    """Print cost beside the score delta, or say why it cannot be compared.

    Silence would be the wrong answer for an unmeasured bundle: the reader is
    comparing two runs on cost, and nothing printed reads as "no difference"
    rather than "not recorded" (#5464).

    Deltas are always b-relative-to-a, matching the argument order rather than
    the ranked order above - a sign that flips depending on which bundle won is
    a number nobody can act on.
    """
    cost_a, cost_b = bundle_a.total_cost, bundle_b.total_cost
    if cost_a is None or cost_b is None:
        unmeasured = [p.name for p, c in ((path_a, cost_a), (path_b, cost_b)) if c is None]
        click.echo("")
        click.echo(f"Cost: not recorded in {', '.join(unmeasured)} — no cost comparison available.")
        return

    click.echo("")
    click.echo(
        f"Cost: {path_a.name} ${cost_a.cost_usd:.4f} over {bundle_a.measured_tasks} measured tasks, "
        f"{path_b.name} ${cost_b.cost_usd:.4f} over {bundle_b.measured_tasks}."
    )
    for label, a_value, b_value, fmt in (
        ("cost", cost_a.cost_usd, cost_b.cost_usd, "$.4f"),
        ("tokens", float(cost_a.tokens), float(cost_b.tokens), ".0f"),
        ("wall time", cost_a.wall_time_s, cost_b.wall_time_s, ".1fs"),
    ):
        delta = b_value - a_value
        sign = "+" if delta >= 0 else "-"
        magnitude = abs(delta)
        rendered = (
            f"${magnitude:.4f}" if fmt == "$.4f" else (f"{magnitude:.0f}" if fmt == ".0f" else f"{magnitude:.1f}s")
        )
        click.echo(f"  {label} delta ({path_b.name} vs {path_a.name}): {sign}{rendered}")

    per_a, per_b = bundle_a.cost_per_verdict, bundle_b.cost_per_verdict
    if per_a is not None and per_b is not None:
        click.echo(f"  cost per verdict: ${per_a:.4f} vs ${per_b:.4f}")


# ---------------------------------------------------------------------------
# Reliability: pass^k floor (issue #2933)
# ---------------------------------------------------------------------------


def _run_reliability(suite_obj: BenchSuite, scheduler: str, k: int, out_path: Path, stub_signer: bool) -> None:
    """Execute the --reliability K path of ``bench run``."""
    from bernstein.eval.bench.reliability import (
        InstallIdentityReliabilitySigner,
        ReliabilityRunner,
        StubReliabilitySigner,
    )
    from bernstein.eval.bench.runner import MockReplayAdapter

    # The reliability path has no production adapter for any suite: it always scores through the
    # synthetic mock, so it can only emit a stub-signed, mock-labelled receipt.
    adapter = MockReplayAdapter()
    if not stub_signer:
        _refuse_install_identity_for_mock(suite_obj, "this reliability receipt")
    click.echo(f"Adapter     : {_MOCK_NOTICE}", err=True)
    runner = ReliabilityRunner(
        suite=suite_obj,
        adapter=adapter,
        scheduler_config={"scheduler": scheduler, "adapter": "mock"},
        k=k,
    )

    click.echo(f"\nRunning tasks x{k} attempts (fixed coordination)…")
    receipt = runner.run()

    if stub_signer:
        receipt = StubReliabilitySigner().sign(receipt)
    else:
        try:
            receipt = InstallIdentityReliabilitySigner().sign(receipt)
        except Exception as exc:
            raise click.ClickException(
                f"Install-identity signing failed ({exc}). Run `bernstein init` to "
                "set up the install identity, or pass --stub-signer for a test-grade receipt."
            ) from exc
    receipt.save(out_path)

    click.echo(f"\npass^{k} floor : {receipt.pass_caret_k * 100:.1f}%  (all {k} attempts must pass)")
    click.echo(f"pass@1        : {receipt.pass_at_1 * 100:.1f}%  (any attempt passed — the ceiling)")
    click.echo(f"coordination  : {'held fixed' if receipt.coordination_ok else 'DIVERGED — floor not admissible'}")
    click.echo(f"Receipt hash  : {receipt.receipt_hash()}")
    click.echo(f"Signed by     : {receipt.signer_fingerprint or '(unsigned)'}")
    click.echo(f"\nReliability receipt written to: {out_path}")


def _reliability_trusted_keys(signer_key_paths: tuple[str, ...]) -> dict[str, bytes]:
    """Build the fingerprint -> public-key map for reliability verification.

    Explicitly provided PEM files are always trusted; the local install
    identity key is added when it already exists on disk (never generated
    as a side effect of verification).
    """
    from bernstein.core.identity.http_signing import install_identity_keyid

    trusted: dict[str, bytes] = {}
    for key_path in signer_key_paths:
        pem = Path(key_path).read_bytes()
        try:
            trusted[install_identity_keyid(pem)] = pem
        except Exception as exc:
            raise click.ClickException(f"Not an Ed25519 public key PEM: {key_path} ({exc})") from exc
    try:
        from bernstein.core.identity.http_signing import default_keystore

        keystore = default_keystore()
        if keystore.directory.exists() and any(keystore.directory.iterdir()):
            _, public_pem = keystore.load_or_generate()
            trusted.setdefault(install_identity_keyid(public_pem), public_pem)
    except Exception:
        # No usable local install identity; explicit --signer-key still works.
        pass
    return trusted


@bench_group.command(name="reliability-verify")
@click.argument("receipt")
@click.option("--suite", default="golden-v1", show_default=True, help="Suite to verify against.")
@click.option(
    "--signer-key",
    "signer_keys",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Trusted Ed25519 public key PEM of the emitting install (repeatable). "
    "The local install identity key is trusted automatically when present.",
)
def bench_reliability_verify(receipt: str, suite: str, signer_keys: tuple[str, ...]) -> None:
    """Verify a reliability receipt by replaying all embedded attempts offline.

    RECEIPT is the path to a reliability receipt .json file.

    Recomputes pass@1 and pass^k from the embedded per-attempt run receipts
    and rejects fabricated floors, stripped attempts, tampered receipt
    bytes, coordination divergence, and signatures that do not verify
    against a trusted key.  Exits 0 on MATCH, 1 otherwise.
    """
    from bernstein.eval.bench.reliability import ReliabilityReceipt, ReliabilityVerifier
    from bernstein.eval.bench.runner import MockReplayAdapter

    receipt_path = Path(receipt)
    if not receipt_path.exists():
        raise click.ClickException(f"Receipt file not found: {receipt_path}")

    receipt_obj = ReliabilityReceipt.load(receipt_path)
    suite_obj = _get_suite(suite)

    # Production: swap MockReplayAdapter for the real scenario_runner adapter.
    adapter = MockReplayAdapter()
    verifier = ReliabilityVerifier(
        suite=suite_obj,
        adapter=adapter,
        trusted_keys=_reliability_trusted_keys(signer_keys),
    )
    result = verifier.verify(receipt_obj)

    click.echo(result.report())
    sys.exit(0 if result.passed else 1)


@bench_group.command(name="reliability-check")
@click.argument("receipt")
@click.option("--suite", default="golden-v1", show_default=True, help="Suite the receipt was produced from.")
@click.option("--task", "task_id", default=None, help="Task id to re-run (default: first task in the receipt).")
@click.option(
    "--attempt",
    "attempt_index",
    type=int,
    default=0,
    show_default=True,
    help="Attempt position to compare: the check replays attempts 0..N in order "
    "on a fresh adapter and compares position N against the recorded attempt N.",
)
def bench_reliability_check(receipt: str, suite: str, task_id: str | None, attempt_index: int) -> None:
    """Re-run one attempt from a reliability receipt and assert coordination byte-identity.

    RECEIPT is the path to a reliability receipt .json file.

    Proves the coordination really was held fixed, so a low pass^k floor is
    attributable to model sampling rather than hidden coordination
    non-determinism.  Exits 0 when the fresh run's coordination is
    byte-identical to the recorded attempt, 1 otherwise (naming the first
    divergent field).
    """
    from bernstein.eval.bench.reliability import ReliabilityReceipt, reliability_check
    from bernstein.eval.bench.runner import MockReplayAdapter

    receipt_path = Path(receipt)
    if not receipt_path.exists():
        raise click.ClickException(f"Receipt file not found: {receipt_path}")

    receipt_obj = ReliabilityReceipt.load(receipt_path)
    suite_obj = _get_suite(suite)

    # Production: swap MockReplayAdapter for the real scenario_runner adapter.
    adapter = MockReplayAdapter()
    result = reliability_check(
        receipt_obj,
        suite_obj,
        adapter,
        task_id=task_id,
        attempt_index=attempt_index,
    )

    click.echo(result.report())
    sys.exit(0 if result.passed else 1)


# ---------------------------------------------------------------------------
# Standalone entry point: bernstein-bench <subcommand>
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bench_group()
