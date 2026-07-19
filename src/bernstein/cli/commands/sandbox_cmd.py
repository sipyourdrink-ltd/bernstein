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

    Proves the receipt is *properly signed and internally consistent* and that
    every snapshot it names (base + winner + every loser) still exists and
    re-hashes correctly in CAS. With ``--expected-keyid`` it additionally proves
    the receipt was signed by that trusted signer. It does NOT prove the receipt
    was appended to the audit chain - that is the audit log's own ``verify``.

    The CLI and the library derive the verdict from one shared definition
    (:func:`~bernstein.core.sandbox.selection_receipt.verify_receipt_full`), so
    the two can never disagree. Exit codes, strongest reason first:

    * ``1`` - invalid signature/consistency, or a blob is **tampered** (present
      but hash-mismatched). Takes precedence over everything below.
    * ``4`` - a blob is present but **unreadable** on this host (a permissions
      problem here, never a claim about the record).
    * ``2`` - authentic and untampered, but a blob is **absent** from CAS
      (GC / retention / restart), so the content re-hash is incomplete.
    * ``3`` - signed, consistent, blobs intact, but **unanchored**: no
      ``--expected-keyid`` was given, so the signer was not checked. A receipt
      re-signed under any other key would look identical, so this never exits 0.
    * ``0`` - signed, anchored to the trusted signer, and every named blob
      re-hashed intact.
    """
    from bernstein.core.persistence.cas_store import CASIntegrityError, CASStore
    from bernstein.core.sandbox.selection_receipt import (
        BLOB_ABSENT,
        BLOB_INTACT,
        BLOB_TAMPERED,
        BLOB_UNREADABLE,
        read_receipt_file,
        verify_receipt_full,
    )

    receipt = read_receipt_file(receipt_path)
    if receipt is None:
        raise click.ClickException(
            f"Could not read a selection receipt from {receipt_path} "
            "(missing, unreadable, or not a valid receipt JSON).",
        )

    cas = CASStore(cas_dir)

    def _blob_status(digest: str) -> str:
        # Classify one blob for verify_receipt_full. Four outcomes, never
        # conflated: tampered (present, wrong hash) is an integrity alarm;
        # absent (GC / retention / restart) is an ordinary operational event;
        # unreadable (a permissions problem *on this host*) is a property of the
        # reader, not the record; intact is the clean case.
        # Ask the store for the path (single source of truth for the shard
        # layout) rather than re-deriving cas_dir / digest[:2] / digest here.
        # Used only for the absent/unreadable disambiguation below; the symlink
        # defence lives in cas.get (O_NOFOLLOW), atomically and race-free - no
        # is_symlink() pre-check here, which would only add a TOCTOU window.
        try:
            blob_path = cas.blob_path(digest)
        except ValueError:
            # Non-64-hex digest; defensive only - verify_receipt_full filters
            # malformed digests before calling this - but never crash here.
            return BLOB_UNREADABLE
        try:
            blob = cas.get(digest, verify=True)
        except CASIntegrityError:
            return BLOB_TAMPERED
        except FileNotFoundError:
            # The blob vanished between the store's open and read (GC /
            # retention race). That is genuinely absent, not a reader-side
            # failure - classify it before the broad OSError below.
            return BLOB_ABSENT
        except (OSError, ValueError):
            # OSError: present but unreadable - a permissions problem on this
            # host, or a symlinked blob cas.get refused to follow (O_NOFOLLOW ->
            # ELOOP). Either way a reader-side failure, never a claim about the
            # record. ValueError: the store rejects a non-64-hex digest;
            # defensive only, since verify_receipt_full already filters malformed
            # digests before calling this - but never crash here.
            return BLOB_UNREADABLE
        if blob is not None:
            return BLOB_INTACT
        # get() returned None. Path.exists() and os.access both *suppress*
        # permission errors, so either would let an unreadable CAS directory
        # masquerade as an absent blob - the exact false accusation #2705 is
        # about. Stat the blob path directly (stat raises, it does not suppress)
        # and let the errno decide: FileNotFoundError is genuinely absent; any
        # other OSError (a permission/traversal failure anywhere on the path) is
        # a reader-side problem, reported as unreadable, never as a claim about
        # the record. A stat that succeeds while get() returned None means the
        # blob is present but could not be read, which is also unreadable.
        try:
            blob_path.stat()
        except FileNotFoundError:
            return BLOB_ABSENT
        except OSError:
            return BLOB_UNREADABLE
        return BLOB_UNREADABLE

    result = verify_receipt_full(receipt, expected_keyid=expected_keyid, blob_status=_blob_status)

    for err in result.signature_errors:
        console.print(f"[red]signature/consistency:[/red] {err}")
    for digest in result.malformed:
        console.print(
            f"[red]malformed:[/red] receipt names a digest that is not 64 hex chars: {digest!r}",
        )
    for digest in result.tampered:
        console.print(f"[red]tampered:[/red] snapshot blob present but hash-mismatched: {digest}")
    for digest in result.unreadable:
        console.print(
            f"[yellow]unreadable:[/yellow] snapshot blob present but not readable on this host: "
            f"{digest} (a permissions problem here, not a claim about the record)",
        )
    for digest in result.absent:
        console.print(
            f"[yellow]cannot-verify:[/yellow] snapshot blob absent from CAS: {digest} "
            "(absent != tampered - likely GC / retention / restart)",
        )
    if not result.anchored:
        console.print(
            "[yellow]unanchored:[/yellow] no --expected-keyid given, so the signer was NOT "
            "checked - a receipt re-signed under any other key would look identical. "
            "Pass --expected-keyid <trusted keyid> to establish trust.",
        )

    if result.verdict == "failed":
        console.print("[red]FAILED[/red] receipt is invalid or a snapshot blob is tampered.")
    elif result.verdict == "unreadable":
        console.print(
            f"[yellow]UNREADABLE[/yellow] {len(result.unreadable)} of {result.digests_checked} "
            "snapshot blob(s) could not be read on this host; verification is incomplete "
            "(a reader-side problem, not a claim about the record).",
        )
    elif result.verdict == "incomplete":
        console.print(
            f"[yellow]INCOMPLETE[/yellow] receipt is authentic and consistent, but "
            f"{len(result.absent)} of {result.digests_checked} snapshot blob(s) are absent from "
            "CAS; content re-hash could not be completed (this is not tampering).",
        )
    elif result.verdict == "unanchored":
        console.print(
            f"[yellow]UNANCHORED[/yellow] receipt is self-consistent and all "
            f"{result.digests_checked} snapshot blob(s) are intact, but the signer was not "
            "verified (no trust anchor); re-run with --expected-keyid to exit 0.",
        )
    else:
        console.print(
            f"[green]OK[/green] receipt signed by the trusted signer + all "
            f"{result.digests_checked} snapshot digests intact "
            "(proves signed + anchored + CAS-intact; not chain-appended).",
        )

    if result.exit_code:
        raise click.exceptions.Exit(code=result.exit_code)


__all__ = [
    "fork_race_cmd",
    "receipt_group",
    "receipt_verify_cmd",
    "sandbox_group",
    "web_test",
]
