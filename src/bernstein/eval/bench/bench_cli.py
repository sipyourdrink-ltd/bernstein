"""
bernstein-bench: CLI entry points (Click).

Registered in src/bernstein/cli/main.py alongside every other subcommand:

    from bernstein.eval.bench.bench_cli import bench_group
    cli.add_command(bench_group)

This exposes:
    bernstein bench run <suite> [--out <path>] [--scheduler <name>] [--stub-signer]
    bernstein bench verify <bundle> [--suite <name>]

Also registered as a standalone script in pyproject.toml:
    bernstein-bench = "bernstein.eval.bench.bench_cli:bench_group"
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Suite registry
# ---------------------------------------------------------------------------


def _get_suite(name: str):
    """Resolve a suite name or .json path to a BenchSuite."""
    from bernstein.eval.bench.golden_suite import build_golden_suite_v1
    from bernstein.eval.bench.suite import BenchSuite

    _BUILTIN = {
        "golden-v1": build_golden_suite_v1,
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
def bench_run(suite: str, out: str, scheduler: str, stub_signer: bool) -> None:
    """Execute a suite and emit a signed submission bundle.

    SUITE is a built-in suite name (e.g. golden-v1) or a path to a .json
    suite file.
    """
    from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
    from bernstein.eval.bench.signer import AgentCardSigner, StubSigner

    suite_obj = _get_suite(suite)
    click.echo(f"Suite       : {suite_obj.version}")
    click.echo(f"Suite hash  : {suite_obj.suite_hash}")
    click.echo(f"Tasks       : {len(suite_obj.tasks)}")

    # Production: swap MockReplayAdapter for the real scenario_runner adapter.
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
    from bernstein.eval.bench.runner import MockReplayAdapter
    from bernstein.eval.bench.verifier import BenchVerifier

    bundle_path = Path(bundle)
    if not bundle_path.exists():
        raise click.ClickException(f"Bundle file not found: {bundle_path}")

    bundle_obj = SubmissionBundle.load(bundle_path)
    suite_obj = _get_suite(suite)

    # Production: swap MockReplayAdapter for the real scenario_runner adapter.
    adapter = MockReplayAdapter()
    verifier = BenchVerifier(suite=suite_obj, adapter=adapter)
    result = verifier.verify(bundle_obj)

    click.echo(result.report())
    sys.exit(0 if result.passed else 1)


# ---------------------------------------------------------------------------
# Standalone entry point: bernstein-bench <subcommand>
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bench_group()
