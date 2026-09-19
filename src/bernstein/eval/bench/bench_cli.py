"""
bernstein-bench: CLI entry points (Click).

Registered in src/bernstein/cli/main.py alongside every other subcommand:

    from bernstein.eval.bench.bench_cli import bench_group
    cli.add_command(bench_group)

This exposes:
    bernstein bench run <suite> [--out <path>] [--scheduler <name>] [--stub-signer]
                        [--reliability K] [--ci] [--sarif-out <path>]
                        [--baseline <path>] [--regression-threshold <float>]
                        [--repo <slug>] [--head-sha <sha>]
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
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from bernstein.eval.bench.suite import BenchSuite

# ---------------------------------------------------------------------------
# Suite registry
# ---------------------------------------------------------------------------


def _get_suite(name: str):
    """Resolve a suite name or .json path to a BenchSuite."""
    from bernstein.eval.bench.golden_suite import build_golden_suite_v1
    from bernstein.eval.bench.suite import BenchSuite
    from bernstein.eval.bench.tool_surface_suite import build_tool_surface_suite

    _BUILTIN = {
        "golden-v1": build_golden_suite_v1,
        "tool-surface-v1": build_tool_surface_suite,
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


def _suite_source_uri(name: str) -> str | None:
    """Repository-relative path the suite's tasks are defined in, for SARIF.

    A .json suite is its own file; a built-in suite lives in the module that
    builds it. Returns ``None`` when neither can be named, so the report
    carries no location rather than an invented one.
    """
    import inspect

    from bernstein.eval.bench import golden_suite, tool_surface_suite

    path = Path(name)
    if path.suffix == ".json" and path.exists():
        return path.as_posix()
    module = {"golden-v1": golden_suite, "tool-surface-v1": tool_surface_suite}.get(name)
    if module is None:
        return None
    source = inspect.getsourcefile(module)
    if source is None:
        return None
    source_path = Path(source).resolve()
    # Name the file relative to the checkout this package lives in
    # (.../<root>/src/bernstein/eval/bench/bench_cli.py -> <root>), so a
    # `bernstein` directory elsewhere in the path cannot mislead the slice.
    try:
        return source_path.relative_to(Path(__file__).resolve().parents[4]).as_posix()
    except (ValueError, IndexError):
        pass
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
    ci: bool,
    sarif_out: str | None,
    baseline: str | None,
    regression_threshold: float,
    repo: str,
    head_sha: str,
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
    from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter, ReplayAdapter
    from bernstein.eval.bench.signer import AgentCardSigner, StubSigner

    suite_obj = _get_suite(suite)
    click.echo(f"Suite       : {suite_obj.version}")
    click.echo(f"Suite hash  : {suite_obj.suite_hash}")
    click.echo(f"Tasks       : {len(suite_obj.tasks)}")

    if reliability_k is not None:
        _run_reliability(suite_obj, scheduler, reliability_k, Path(out), stub_signer)
        return

    # Production: swap MockReplayAdapter for the real scenario_runner adapter.
    adapter: ReplayAdapter
    if suite_obj.version == "tool-surface-v1":
        from bernstein.eval.bench.tool_surface_suite import ToolSurfaceReplayAdapter

        adapter = ToolSurfaceReplayAdapter()
    else:
        adapter = MockReplayAdapter()
    runner = BenchRunner(
        suite=suite_obj,
        adapter=adapter,
        scheduler_config={"scheduler": scheduler},
    )

    click.echo("\nRunning tasks…")
    bundle = runner.run()

    signer = StubSigner() if stub_signer else AgentCardSigner()
    bundle = signer.sign(bundle)

    out_path = Path(out)
    bundle.save(out_path)

    click.echo(f"\nScore       : {bundle.overall_score * 100:.1f}%")
    click.echo(f"Pass rate   : {bundle.pass_rate * 100:.1f}%")
    click.echo(f"Bundle hash : {bundle.bundle_hash()}")
    click.echo(f"Signed by   : {bundle.signer_fingerprint or '(unsigned)'}")
    click.echo(f"\nBundle written to: {out_path}")

    if ci or sarif_out:
        from bernstein.eval.bench.ci import evaluate_ci_scorecard, post_bench_check_run
        from bernstein.eval.bench.sarif import bundle_to_sarif
        from bernstein.eval.bench.verifier import BenchVerifier
        from bernstein.github_app.check_runs import CheckRunClient

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
            if not base_path.is_file():
                raise click.ClickException(f"Baseline bundle not found: {base_path}")
            try:
                baseline_bundle = SubmissionBundle.load(base_path)
            except Exception as exc:
                baseline_problem = (
                    f"Baseline bundle {base_path.name} could not be loaded "
                    f"({type(exc).__name__}: {exc}). Result is neutral."
                )

        verifier = BenchVerifier(suite=suite_obj, adapter=adapter)
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


# ---------------------------------------------------------------------------
# bernstein bench verify
# ---------------------------------------------------------------------------


@bench_group.command(name="verify")
@click.argument("bundle")
@click.option("--suite", default="golden-v1", show_default=True, help="Suite to verify against.")
def bench_verify(bundle: str, suite: str) -> None:
    """Verify a bundle by replaying every task receipt offline.

    BUNDLE is the path to a submission bundle .json file.

    Exits 0 on MATCH, 1 on any divergence or fabricated score.
    """
    from bernstein.eval.bench.bundle import SubmissionBundle
    from bernstein.eval.bench.runner import MockReplayAdapter, ReplayAdapter
    from bernstein.eval.bench.verifier import BenchVerifier

    bundle_path = Path(bundle)
    if not bundle_path.exists():
        raise click.ClickException(f"Bundle file not found: {bundle_path}")

    bundle_obj = SubmissionBundle.load(bundle_path)
    suite_obj = _get_suite(suite)

    # Production: swap MockReplayAdapter for the real scenario_runner adapter.
    adapter: ReplayAdapter
    if suite_obj.version == "tool-surface-v1":
        from bernstein.eval.bench.tool_surface_suite import ToolSurfaceReplayAdapter

        adapter = ToolSurfaceReplayAdapter()
    else:
        adapter = MockReplayAdapter()
    verifier = BenchVerifier(suite=suite_obj, adapter=adapter)
    result = verifier.verify(bundle_obj)

    click.echo(result.report())
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
def bench_compare(a: str, b: str, allow_harness_drift: bool) -> None:
    """Compare two submission bundles, ranking by score.

    A and B are paths to submission bundle .json files.

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

    fp_a, fp_b = bundle_a.harness_fingerprint, bundle_b.harness_fingerprint
    if fp_a != fp_b:
        differing = sorted(
            key
            for key in set(bundle_a.scheduler_config) | set(bundle_b.scheduler_config)
            if bundle_a.scheduler_config.get(key) != bundle_b.scheduler_config.get(key)
        )
        click.echo(f"Harness fingerprints differ: {fp_a[:12]}… vs {fp_b[:12]}…")
        click.echo(f"Differing harness settings: {', '.join(differing)}")
        if not allow_harness_drift:
            click.echo(
                "Refusing to rank: a score gap across differing harness settings "
                "is a harness change, not a model change. "
                "Pass --allow-harness-drift to rank anyway."
            )
            sys.exit(1)
        click.echo("--allow-harness-drift: ranking anyway.")
    else:
        click.echo(f"Harness fingerprint: {fp_a} (match)")

    ordered = sorted([(path_a, bundle_a), (path_b, bundle_b)], key=lambda p: -p[1].overall_score)
    click.echo("")
    for rank, (path, bundle) in enumerate(ordered, start=1):
        click.echo(
            f"{rank}. {path.name}: score {bundle.overall_score * 100:.1f}%, "
            f"pass rate {bundle.pass_rate * 100:.1f}%, {len(bundle.task_results)} tasks"
        )


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

    # Production: swap MockReplayAdapter for the real scenario_runner adapter.
    adapter = MockReplayAdapter()
    runner = ReliabilityRunner(
        suite=suite_obj,
        adapter=adapter,
        scheduler_config={"scheduler": scheduler},
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
