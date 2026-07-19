"""``bernstein sandbox`` - sandbox-adjacent operator commands.

Subcommands:

* ``sandbox web-test`` - drive a Playwright self-test against a dev
  server URL using scenarios declared in a YAML file. Artefacts land
  under ``.sdd/sandbox/<task-id>/``.

The CLI is intentionally thin: it loads scenarios, invokes
:class:`bernstein.core.sandbox.playwright_runner.PlaywrightRunner`, and
prints the structured self-test block on stdout. The same block is
intended to be fed back into the agent's next prompt.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import click

from bernstein.cli.helpers import console
from bernstein.core.sandbox.playwright_runner import (
    PlaywrightRunner,
    PlaywrightScenarioError,
    PlaywrightUnavailableError,
    load_scenarios,
)

if TYPE_CHECKING:
    from bernstein.core.sandbox.backend import SandboxSession
    from bernstein.core.sandbox.playwright_runner import PlaywrightRunResult

logger = logging.getLogger(__name__)


_DEFAULT_OUTPUT_ROOT = Path(".sdd/sandbox")
# Allowed task_id shape: must be a single safe slug (letters, digits, dot,
# hyphen, underscore). Rejects path separators and traversal sequences so
# `_DEFAULT_OUTPUT_ROOT / task_id` cannot escape the sandbox root.
_TASK_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@click.group("sandbox")
def sandbox_group() -> None:
    """Sandbox-adjacent operator commands."""


@sandbox_group.command("web-test")
@click.argument("task_id", type=str)
@click.option(
    "--url",
    "base_url",
    required=True,
    help="Base URL of the dev server (e.g. http://localhost:5173).",
)
@click.option(
    "--scenarios",
    "scenarios_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the scenarios YAML file.",
)
@click.option(
    "--output-dir",
    "output_dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help=("Directory for screenshots, console logs, and the run summary. Defaults to .sdd/sandbox/<task-id>/."),
)
@click.option(
    "--task-description",
    "task_description",
    default="",
    help="Task description forwarded to the LLM judge prompt.",
)
@click.option(
    "--judge/--no-judge",
    default=False,
    show_default=True,
    help="Run the LLM judge against the scenario result.",
)
@click.option(
    "--judge-model",
    default="anthropic/claude-sonnet-4",
    show_default=True,
    help="LLM judge model identifier.",
)
@click.option(
    "--judge-provider",
    default="openrouter_free",
    show_default=True,
    help="LLM judge provider.",
)
@click.option(
    "--headless/--headed",
    default=True,
    show_default=True,
    help="Whether to launch Chromium headless.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Print the full run summary as JSON instead of the self-test block.",
)
def web_test(
    task_id: str,
    base_url: str,
    scenarios_path: Path,
    output_dir: Path | None,
    task_description: str,
    judge: bool,
    judge_model: str,
    judge_provider: str,
    headless: bool,
    as_json: bool,
) -> None:
    """Run Playwright scenarios and emit a structured self-test block."""
    if not _TASK_ID_PATTERN.fullmatch(task_id):
        raise click.BadParameter(
            "task_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}: no path separators or traversal segments allowed.",
            param_hint="TASK_ID",
        )

    try:
        scenarios = load_scenarios(scenarios_path)
    except (FileNotFoundError, PlaywrightScenarioError) as exc:
        raise click.ClickException(f"Scenario load failed: {exc}") from exc

    target_dir = output_dir or _DEFAULT_OUTPUT_ROOT / task_id
    runner = PlaywrightRunner(
        base_url=base_url,
        output_dir=target_dir,
        headless=headless,
    )

    judge_instance = None
    if judge:
        # Imported lazily so the CLI does not eagerly load the LLM client
        # when --no-judge is used.
        from bernstein.eval.judge import EvalJudge

        judge_instance = EvalJudge(model=judge_model, provider=judge_provider)

    try:
        result: PlaywrightRunResult = asyncio.run(
            runner.run(
                scenarios,
                task_description=task_description,
                judge=judge_instance,
            )
        )
    except PlaywrightUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        summary_path = Path(result.summary_path)
        console.print(summary_path.read_text(encoding="utf-8"))
    else:
        console.print(result.to_self_test_block())
        console.print(f"\n[dim]summary: {result.summary_path}[/dim]")

    if not result.passed:
        raise click.exceptions.Exit(code=1)


# ---------------------------------------------------------------------------
# fork-and-race (#2613)
# ---------------------------------------------------------------------------


_DEFAULT_CAS_DIR = Path(".sdd/cas")
_DEFAULT_SELECTION_KEY = Path(".sdd/keys/selection.key")
_DEFAULT_AUDIT_DIR = Path(".sdd/audit")


@sandbox_group.command("fork-race")
@click.option("--base", "base_digest", required=True, help="Base snapshot SHA-256 digest to fork every candidate from.")
@click.option("--k", "k", type=click.IntRange(min=1), default=3, show_default=True, help="Number of candidates.")
@click.option(
    "--cmd", "cmd", required=True, help="Shell command run as each candidate (reads $BERNSTEIN_CANDIDATE_INDEX)."
)
@click.option(
    "--out",
    "out_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to write the signed receipt JSON.",
)
@click.option(
    "--cas-dir",
    "cas_dir",
    default=_DEFAULT_CAS_DIR,
    type=click.Path(file_okay=False, path_type=Path),
    show_default=True,
)
@click.option(
    "--key",
    "key_path",
    default=_DEFAULT_SELECTION_KEY,
    type=click.Path(dir_okay=False, path_type=Path),
    show_default=True,
    help="Ed25519 signing key (created 0600 on first use).",
)
@click.option(
    "--audit-dir",
    "audit_dir",
    default=_DEFAULT_AUDIT_DIR,
    type=click.Path(file_okay=False, path_type=Path),
    show_default=True,
)
def fork_race_cmd(
    base_digest: str,
    k: int,
    cmd: str,
    out_path: Path,
    cas_dir: Path,
    key_path: Path,
    audit_dir: Path,
) -> None:
    """Fork K microVM candidates from one base snapshot and emit a signed receipt.

    Requires a microVM-capable host (KVM + kernel/rootfs); on an unsupported
    host it fails loudly rather than degrading isolation.
    """
    from bernstein.core.orchestration.best_of_n import CandidateResult
    from bernstein.core.persistence.cas_store import CASStore
    from bernstein.core.sandbox.backends._vmmonitor import MicroVMUnavailableError
    from bernstein.core.sandbox.backends.microvm import MicroVMSandboxBackend
    from bernstein.core.sandbox.fork_race import fork_race
    from bernstein.core.sandbox.selection_receipt import (
        load_or_create_signing_key,
        write_receipt,
    )
    from bernstein.core.security.audit import AuditLog

    cas = CASStore(cas_dir)
    # Validate the base digest through CAS *before* any side-effectful state:
    # a malformed or unknown --base must fail cleanly without minting a signing
    # key, creating the audit directory, or booting a candidate. cas.has()
    # raises ValueError on a non-hex/wrong-length digest and returns False on a
    # well-formed-but-absent one; both become a clean CLI error here (and this
    # also turns the otherwise-uncaught KeyError from resume() into one).
    try:
        base_present = cas.has(base_digest)
    except ValueError as exc:
        raise click.ClickException(f"invalid base snapshot digest {base_digest!r}: {exc}") from exc
    if not base_present:
        raise click.ClickException(f"base snapshot digest not found in CAS ({cas_dir}): {base_digest}")
    signing_key = load_or_create_signing_key(key_path)
    audit_log = AuditLog(audit_dir)

    async def run_candidate(session: SandboxSession, index: int) -> CandidateResult:
        result = await session.exec(
            ["sh", "-lc", cmd],
            env={"BERNSTEIN_CANDIDATE_INDEX": str(index)},
        )
        return CandidateResult(task_id=f"candidate-{index}", tests_passing=result.exit_code == 0)

    try:
        # Constructed inside the try so any future preflight in the backend
        # constructor surfaces as a clean ClickException, not a raw traceback.
        backend = MicroVMSandboxBackend(cas=cas)
        receipt = asyncio.run(
            fork_race(
                backend=backend,
                base_snapshot_digest=base_digest,
                run_candidate=run_candidate,
                k=k,
                signing_key=signing_key,
                audit_log=audit_log,
                audit_lock_path=audit_dir / ".fork_race.lock",
            ),
        )
    except MicroVMUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc

    write_receipt(out_path, receipt)
    console.print(f"[green]winner:[/green] {receipt.winner_task_id} ({receipt.winner_snapshot_digest[:12]})")
    console.print(f"[dim]receipt: {out_path}[/dim]")


@sandbox_group.group("receipt")
def receipt_group() -> None:
    """Inspect and verify fork-race selection receipts."""


@receipt_group.command("verify")
@click.argument("receipt_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--cas-dir",
    "cas_dir",
    default=_DEFAULT_CAS_DIR,
    type=click.Path(file_okay=False, path_type=Path),
    show_default=True,
)
@click.option(
    "--expected-keyid",
    "expected_keyid",
    default=None,
    help=(
        "Require the receipt to be signed by this trusted keyid (sha256 of the "
        "signer's DER public key). Without it, any self-consistent receipt "
        "verifies - including one re-signed under an attacker's own key."
    ),
)
def receipt_verify_cmd(receipt_path: Path, cas_dir: Path, expected_keyid: str | None) -> None:
    """Verify a receipt's signature and re-hash every snapshot blob in CAS.

    Proves the receipt is *properly signed and internally consistent* and
    that every snapshot it names (base + winner + every loser) still exists
    and re-hashes correctly in CAS. With ``--expected-keyid`` it additionally
    proves the receipt was signed by that trusted signer. It does NOT prove
    the receipt was appended to the audit chain - that is the audit log's own
    ``verify``.

    Exit codes give three distinct answers (an absent blob is an operational
    event, not proof of tampering, and must not read as one):

    * ``0`` - signed, consistent, and every named blob re-hashed intact.
    * ``1`` - invalid signature/consistency, or a blob is **tampered**
      (present but hash-mismatched).
    * ``2`` - authentic and untampered, but a blob is **absent** from CAS, so
      the content re-hash could not be completed (verification incomplete).
    """
    from bernstein.core.persistence.cas_store import CASIntegrityError, CASStore
    from bernstein.core.sandbox.selection_receipt import (
        read_receipt_file,
        snapshot_digests,
        verify_receipt,
    )

    receipt = read_receipt_file(receipt_path)
    if receipt is None:
        raise click.ClickException(f"Could not read a selection receipt from {receipt_path}")

    verification = verify_receipt(receipt, expected_keyid=expected_keyid)
    for err in verification.errors:
        console.print(f"[red]signature/consistency:[/red] {err}")

    cas = CASStore(cas_dir)
    # Distinguish two very different CAS outcomes (a hard lesson from the v3.7.1
    # hardening wave): a blob that is *present but hash-mismatched* is tampering,
    # while a blob that is simply *absent* is an ordinary operational event (GC,
    # log retention, a restart). Conflating them turns a retry into a permanent
    # "tampered" verdict on an append-only chain, so they get different answers.
    tampered: list[str] = []
    absent: list[str] = []
    digests = snapshot_digests(receipt)
    for digest in digests:
        try:
            blob = cas.get(digest, verify=True)
        except CASIntegrityError as exc:
            tampered.append(str(exc))
            continue
        if blob is None:
            absent.append(digest)

    for err in verification.errors:
        console.print(f"[red]signature/consistency:[/red] {err}")
    for err in tampered:
        console.print(f"[red]tampered:[/red] {err}")
    for digest in absent:
        console.print(
            f"[yellow]cannot-verify:[/yellow] snapshot blob absent from CAS: {digest} "
            "(absent != tampered - likely GC / retention / restart)",
        )

    # Three answers, three exit codes (see the command docstring).
    if not verification.ok or tampered:
        console.print("[red]FAILED[/red] receipt is invalid or a snapshot blob is tampered.")
        raise click.exceptions.Exit(code=1)
    if absent:
        console.print(
            f"[yellow]INCOMPLETE[/yellow] receipt is authentic and consistent, but "
            f"{len(absent)} of {len(digests)} snapshot blob(s) are absent from CAS; "
            "content re-hash could not be completed (this is not tampering).",
        )
        raise click.exceptions.Exit(code=2)
    console.print(
        f"[green]OK[/green] receipt signed + all {len(digests)} snapshot digests intact "
        "(proves signed + CAS-intact; not chain-appended).",
    )


__all__ = [
    "fork_race_cmd",
    "receipt_group",
    "receipt_verify_cmd",
    "sandbox_group",
    "web_test",
]
