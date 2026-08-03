"""
bernstein-bench: CLI entry points (Click).

Registered in src/bernstein/cli/main.py alongside every other subcommand:

    from bernstein.eval.bench.bench_cli import bench_group
    cli.add_command(bench_group)

This exposes:
    bernstein bench run <suite> [--out <path>] [--scheduler <name>] [--stub-signer]
                        [--reliability K]
    bernstein bench verify <bundle> [--suite <name>]
    bernstein bench reliability-verify <receipt> [--suite <name>]
    bernstein bench reliability-check <receipt> [--suite <name>] [--task <id>] [--attempt N]

Also registered as a standalone script in pyproject.toml:
    bernstein-bench = "bernstein.eval.bench.bench_cli:bench_group"
"""

from __future__ import annotations

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
def bench_run(suite: str, out: str, scheduler: str, stub_signer: bool, reliability_k: int | None) -> None:
    """Execute a suite and emit a signed submission bundle.

    SUITE is a built-in suite name (e.g. golden-v1) or a path to a .json
    suite file.

    With --reliability K, every task is run K times with the scheduler
    config held byte-identical across attempts, and the output is a signed
    reliability receipt reporting pass@1 (any attempt passed) and pass^k
    (all K attempts passed — the headline floor).
    """
    from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
    from bernstein.eval.bench.signer import AgentCardSigner, StubSigner

    suite_obj = _get_suite(suite)
    click.echo(f"Suite       : {suite_obj.version}")
    click.echo(f"Suite hash  : {suite_obj.suite_hash}")
    click.echo(f"Tasks       : {len(suite_obj.tasks)}")

    if reliability_k is not None:
        _run_reliability(suite_obj, scheduler, reliability_k, Path(out), stub_signer)
        return

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
# Reliability: pass^k floor (issue #2933)
# ---------------------------------------------------------------------------


def _run_reliability(suite_obj: BenchSuite, scheduler: str, k: int, out_path: Path, stub_signer: bool) -> None:
    """Execute the --reliability K path of ``bench run``."""
    from bernstein.eval.bench.reliability import (
        AgentCardReliabilitySigner,
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

    signer = StubReliabilitySigner() if stub_signer else AgentCardReliabilitySigner()
    receipt = signer.sign(receipt)
    receipt.save(out_path)

    click.echo(f"\npass^{k} floor : {receipt.pass_caret_k * 100:.1f}%  (all {k} attempts must pass)")
    click.echo(f"pass@1        : {receipt.pass_at_1 * 100:.1f}%  (any attempt passed — the ceiling)")
    click.echo(f"coordination  : {'held fixed' if receipt.coordination_ok else 'DIVERGED — floor not admissible'}")
    click.echo(f"Receipt hash  : {receipt.receipt_hash()}")
    click.echo(f"Signed by     : {receipt.signer_fingerprint or '(unsigned)'}")
    click.echo(f"\nReliability receipt written to: {out_path}")


@bench_group.command(name="reliability-verify")
@click.argument("receipt")
@click.option("--suite", default="golden-v1", show_default=True, help="Suite to verify against.")
def bench_reliability_verify(receipt: str, suite: str) -> None:
    """Verify a reliability receipt by replaying all embedded attempts offline.

    RECEIPT is the path to a reliability receipt .json file.

    Recomputes pass@1 and pass^k from the embedded per-attempt run receipts
    and rejects fabricated floors, stripped attempts, tampered receipt
    bytes, and coordination divergence.  Exits 0 on MATCH, 1 otherwise.
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
    verifier = ReliabilityVerifier(suite=suite_obj, adapter=adapter)
    result = verifier.verify(receipt_obj)

    click.echo(result.report())
    sys.exit(0 if result.passed else 1)


@bench_group.command(name="reliability-check")
@click.argument("receipt")
@click.option("--suite", default="golden-v1", show_default=True, help="Suite the receipt was produced from.")
@click.option("--task", "task_id", default=None, help="Task id to re-run (default: first task in the receipt).")
@click.option("--attempt", "attempt_index", type=int, default=0, show_default=True, help="Attempt index to compare.")
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
