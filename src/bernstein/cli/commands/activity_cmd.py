"""``bernstein activity``: verify typed activity boundary crossings (#2311).

Bernstein's deterministic scheduler dispatches any agent modality -- research,
browser/computer-use, data, ops, coding -- behind one typed activity boundary,
anchoring each crossing into the run's canonical event journal as an
``activity.result`` entry that pins the ``evidence_set_hash`` (a pure function of
the content-addressed evidence the activity gathered).

    bernstein activity verify <run>

``verify`` walks the run journal, recomputes each anchored activity's
``evidence_set_hash`` from its pinned observation hashes, and -- when the run's
content store is present -- reattaches the evidence bytes and re-verifies every
content hash. A tampered journal entry or a divergent stored blob fails. Exit
codes: 0 = verified, 1 = no run / no activity, 2 = mismatch (tamper).

The ``browser`` subgroup submits and inspects browser activities -- site checks
and UI flows -- alongside coding tasks::

    bernstein activity browser run --flow flow.json --run <run> --stage <stage>
    bernstein activity browser verify <run> --stage <stage>

``run`` drives the flow through the browser worker and dispatches the result down
the same deterministic path a coding spawn uses. With ``--recording`` it drives a
recorded observation tape instead of a live browser, which is how a completed run
is re-executed offline to prove the action sequence and verdict reproduce
byte-for-byte. ``verify`` resolves a completed run's anchored chain from the
content store alone, naming the exact step index or check id on any divergence.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from bernstein.cli.helpers import console

if TYPE_CHECKING:
    from bernstein.core.orchestration.browser_check import BrowserFlowVerdict
    from bernstein.core.orchestration.browser_driver import PageState
    from bernstein.core.orchestration.browser_worker import BrowserBudget, CheckSpec, FlowStep


@click.group(name="activity")
def activity_group() -> None:
    """Typed activity-boundary tooling for any agent modality.

    \b
    Examples:
      bernstein activity verify run-42
      bernstein activity verify run-42 --json
      bernstein activity browser run --flow checkout.json --run run-42 --stage browser-0
      bernstein activity browser verify run-42 --stage browser-0
    """


@activity_group.command("verify")
@click.argument("run")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def activity_verify_cmd(run: str, workdir: str, as_json: bool) -> None:
    """Recompute and re-verify every activity anchored in RUN's journal.

    Confirms the journal's Merkle chain is intact, recomputes each activity's
    ``evidence_set_hash`` from its pinned observation hashes, and reattaches the
    evidence bytes from the run's content store (when present). Exit codes:
    0 = verified, 1 = no run / no activity, 2 = mismatch (tamper).
    """
    from bernstein.core.orchestration.activity_modalities import (
        ContentStore,
        verify_run_activities,
    )

    root = Path(workdir).resolve()
    sdd_dir = root / ".sdd"
    cas_dir = sdd_dir / "cas"
    store = ContentStore(cas_dir) if cas_dir.exists() else None

    result = verify_run_activities(sdd_dir, run_id=run, store=store)

    if as_json:
        payload = {
            "run": result.run_id,
            "found": result.found,
            "ok": result.ok,
            "chain_ok": result.chain_ok,
            "reason": result.reason,
            "stages": [
                {
                    "stage_id": s.stage_id,
                    "kind": s.kind,
                    "ok": s.ok,
                    "evidence_reattached": s.evidence_reattached,
                    "signed_receipt_verified": s.signed_receipt_verified,
                    "claim_verdicts": [cv.to_dict() for cv in s.claim_verdicts],
                    "browser_verdict": _browser_verdict_payload(s.browser_verdict),
                    "reason": s.reason,
                }
                for s in result.stages
            ],
        }
        console.print_json(json.dumps(payload))
    else:
        console.print()
        console.print(f"[bold]Activity verify[/bold] run={result.run_id}")
        if not result.found:
            console.print(f"[yellow]NO ACTIVITY[/yellow] -- {result.reason}")
        else:
            for stage in result.stages:
                if stage.ok:
                    tag = "[green]OK[/green]"
                    notes = []
                    if stage.evidence_reattached:
                        notes.append("evidence reattached")
                    if stage.signed_receipt_verified:
                        notes.append("signed receipt verified")
                    if stage.claim_verdicts:
                        notes.append(f"{len(stage.claim_verdicts)} citations resolved")
                    if stage.browser_verdict is not None:
                        notes.append(
                            f"{len(stage.browser_verdict.steps)} steps replayed, "
                            f"{len(stage.browser_verdict.checks)} checks recomputed"
                        )
                    extra = f" ({', '.join(notes)})" if notes else ""
                    console.print(f"  {tag} {stage.stage_id} [{stage.kind}]{extra}")
                else:
                    console.print(f"  [red]MISMATCH[/red] {stage.stage_id} [{stage.kind}] -- {stage.reason}")
                for claim in stage.claim_verdicts:
                    claim_tag = "[green]cite OK[/green]" if claim.ok else "[red]cite FAIL[/red]"
                    detail = f" -- {claim.reason}" if not claim.ok and claim.reason else ""
                    console.print(f"    {claim_tag} claim={claim.claim_id} ({claim.citations_checked} checked){detail}")
            if result.ok:
                console.print("[green]verified[/green] -- every activity reconstructs from the journal.")

    if not result.found:
        raise SystemExit(1)
    if not result.ok:
        raise SystemExit(2)
    raise SystemExit(0)


def _browser_verdict_payload(verdict: BrowserFlowVerdict | None) -> dict[str, Any] | None:
    """Return the JSON projection of a browser flow verdict, or ``None``."""
    if verdict is None:
        return None
    return {
        "ok": verdict.ok,
        "head_anchor_ok": verdict.head_anchor_ok,
        "reason": verdict.reason,
        "steps": [s.to_dict() for s in verdict.steps],
        "checks": [c.to_dict() for c in verdict.checks],
    }


# ---------------------------------------------------------------------------
# browser activities
# ---------------------------------------------------------------------------


@activity_group.group("browser")
def browser_group() -> None:
    """Submit and inspect browser activities: site checks and UI flows.

    \b
    Examples:
      bernstein activity browser run --flow checkout.json --run run-42 --stage browser-0
      bernstein activity browser run --flow checkout.json --recording tape.json --run run-42
      bernstein activity browser verify run-42 --stage browser-0
    """


def _parse_checks(rows: object, *, where: str) -> tuple[CheckSpec, ...]:
    """Parse a list of check specs from a flow document."""
    from bernstein.core.orchestration.browser_check import CheckKind
    from bernstein.core.orchestration.browser_worker import CheckSpec as Spec

    if rows in (None, []):
        return ()
    if not isinstance(rows, list):
        raise click.BadParameter(f"{where}: checks must be a list")
    specs: list[Spec] = []
    for row in rows:
        if not isinstance(row, dict):
            raise click.BadParameter(f"{where}: each check must be an object")
        try:
            kind = CheckKind(str(row.get("kind", "")))
        except ValueError as exc:
            raise click.BadParameter(f"{where}: unknown check kind {row.get('kind')!r}") from exc
        specs.append(Spec(check_id=str(row.get("id", "")), kind=kind, operand=str(row.get("operand", ""))))
    return tuple(specs)


def _parse_flow(
    document: dict[str, Any],
) -> tuple[str, str, tuple[FlowStep, ...], tuple[CheckSpec, ...], BrowserBudget]:
    """Parse a flow document into the worker's typed inputs."""
    from bernstein.core.agents.computer_use import Action, ActionKind
    from bernstein.core.orchestration.browser_worker import BrowserBudget as Budget
    from bernstein.core.orchestration.browser_worker import FlowStep as Step

    flow_id = str(document.get("flow_id", "")).strip()
    if not flow_id:
        raise click.BadParameter("flow document must carry a non-empty flow_id")
    start_url = str(document.get("start_url", ""))

    raw_steps = document.get("steps", [])
    if not isinstance(raw_steps, list):
        raise click.BadParameter("flow document steps must be a list")
    steps: list[Step] = []
    for position, row in enumerate(raw_steps):
        if not isinstance(row, dict):
            raise click.BadParameter(f"step {position}: must be an object")
        raw_action = row.get("action", {})
        if not isinstance(raw_action, dict):
            raise click.BadParameter(f"step {position}: action must be an object")
        try:
            kind = ActionKind(str(raw_action.get("kind", "")))
        except ValueError as exc:
            raise click.BadParameter(f"step {position}: unknown action kind {raw_action.get('kind')!r}") from exc
        steps.append(
            Step(
                action=Action(
                    kind=kind,
                    target=str(raw_action.get("target", "")),
                    # Only a digest ever travels in a flow document, so a stored
                    # flow never carries the secret a form step types.
                    value_digest=str(raw_action.get("value_digest", "")),
                ),
                checks=_parse_checks(row.get("checks"), where=f"step {position}"),
            )
        )

    raw_budget = document.get("budget", {})
    budget_row = raw_budget if isinstance(raw_budget, dict) else {}
    try:
        budget = Budget(
            max_steps=int(budget_row.get("max_steps", 64)),
            max_observation_bytes=int(budget_row.get("max_observation_bytes", 64 * 1024 * 1024)),
        )
    except (TypeError, ValueError) as exc:
        raise click.BadParameter(f"flow document budget is malformed: {exc}") from exc
    final_checks = _parse_checks(document.get("final_checks"), where="final_checks")
    return flow_id, start_url, tuple(steps), final_checks, budget


def _parse_recording(document: dict[str, Any]) -> tuple[PageState, ...]:
    """Parse a recorded observation tape into page states."""
    from bernstein.core.orchestration.browser_driver import PageState as State

    raw_frames = document.get("frames", [])
    if not isinstance(raw_frames, list) or not raw_frames:
        raise click.BadParameter("recording must carry a non-empty frames list")
    frames: list[State] = []
    for position, row in enumerate(raw_frames):
        if not isinstance(row, dict):
            raise click.BadParameter(f"frame {position}: must be an object")
        try:
            screenshot = base64.b64decode(str(row.get("screenshot_b64", "")), validate=True)
            dom = base64.b64decode(str(row.get("dom_b64", "")), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise click.BadParameter(f"frame {position}: screenshot_b64 / dom_b64 must be base64") from exc
        frames.append(State(url=str(row.get("url", "")), screenshot=screenshot, dom=dom))
    return tuple(frames)


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    """Read and parse a JSON document, refusing anything that is not an object."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise click.BadParameter(f"{label} is not readable JSON: {type(exc).__name__}") from exc
    if not isinstance(document, dict):
        raise click.BadParameter(f"{label} must be a JSON object")
    return document


@browser_group.command("run")
@click.option("--flow", "flow_path", type=click.Path(dir_okay=False, exists=True), required=True, help="Flow document.")
@click.option("--run", "run_id", required=True, help="Run the activity anchors into.")
@click.option("--stage", "stage_id", default="browser-0", show_default=True, help="Scheduler stage id.")
@click.option(
    "--recording",
    "recording_path",
    type=click.Path(dir_okay=False, exists=True),
    default=None,
    help="Drive a recorded observation tape instead of a live browser (offline, deterministic).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def browser_run_cmd(
    flow_path: str,
    run_id: str,
    stage_id: str,
    recording_path: str | None,
    workdir: str,
    as_json: bool,
) -> None:
    """Drive a browser FLOW and anchor the result into RUN's journal.

    Every observation and action is content-addressed and folded into a Merkle
    chain whose head is the run identity, then dispatched down the same path a
    coding spawn uses. With ``--recording`` the flow runs against a recorded tape,
    so a completed run re-executes offline and must reproduce a byte-identical
    action sequence and verdict. Exit codes: 0 = completed with every check
    passing, 2 = completed with a failing check, 3 = refused / failed / timed out.
    """
    from bernstein.core.orchestration.activity import ActivityRejected, TerminalState, dispatch_activity
    from bernstein.core.orchestration.activity_modalities import ContentStore
    from bernstein.core.orchestration.browser_driver import (
        BrowserDriver,
        RecordedBrowserDriver,
        browser_use_driver,
    )
    from bernstein.core.orchestration.browser_worker import BrowserBudgetExceeded, BrowserWorker
    from bernstein.core.replay.journal import EventJournal

    root = Path(workdir).resolve()
    sdd_dir = root / ".sdd"
    flow_id, start_url, steps, final_checks, budget = _parse_flow(_load_json(Path(flow_path), label="flow document"))

    if recording_path is not None:
        frames = _parse_recording(_load_json(Path(recording_path), label="recording"))

        def build(profile_dir: Path) -> BrowserDriver:
            return RecordedBrowserDriver(frames, profile_dir=profile_dir)
    else:

        def build(profile_dir: Path) -> BrowserDriver:
            return browser_use_driver(profile_dir=profile_dir)

    worker = BrowserWorker(
        store=ContentStore(sdd_dir / "cas"),
        budget=budget,
        profile_root=sdd_dir / "browser-profiles",
    )
    try:
        run = worker.run(
            flow_id=flow_id,
            start_url=start_url,
            steps=steps,
            driver_factory=build,
            final_checks=final_checks,
        )
    except BrowserBudgetExceeded as exc:
        # A cost cap is a refusal before the work happens, so nothing is anchored
        # and there is no partial result to report.
        raise click.ClickException(f"browser flow refused by its cost cap: {exc}") from exc
    except ActivityRejected as exc:
        raise click.ClickException(f"browser flow report refused at the activity boundary: {exc}") from exc
    dispatch_activity(run.result, stage_id=stage_id, journal=EventJournal(run_id=run_id, sdd_dir=sdd_dir))

    failed = [c.check_id for c in run.report.checks if not c.passed]
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "run": run_id,
                    "stage_id": stage_id,
                    "flow_id": run.report.flow_id,
                    "terminal_state": run.result.terminal_state.value,
                    "reason_code": run.result.reason_code,
                    "steps_executed": run.steps_executed,
                    "head_anchor": run.report.head_anchor,
                    "artifact_hash": run.result.artifact_hash,
                    "evidence_set_hash": run.result.evidence_set_hash,
                    "failed_checks": failed,
                }
            )
        )
    else:
        console.print()
        console.print(f"[bold]Browser activity[/bold] flow={run.report.flow_id} run={run_id} stage={stage_id}")
        console.print(f"  terminal={run.result.terminal_state.value} reason={run.result.reason_code}")
        console.print(f"  steps anchored: {run.steps_executed}, head anchor: {run.report.head_anchor or '(genesis)'}")
        for check in run.report.checks:
            tag = "[green]PASS[/green]" if check.passed else "[red]FAIL[/red]"
            console.print(f"  {tag} {check.check_id} ({check.kind}) at step {check.step_index}")
        if run.result.terminal_state is TerminalState.COMPLETED and not failed:
            console.print("[green]completed[/green] -- every check passed against anchored evidence.")

    if run.result.terminal_state is not TerminalState.COMPLETED:
        raise SystemExit(3)
    raise SystemExit(2 if failed else 0)


@browser_group.command("verify")
@click.argument("run")
@click.option("--stage", "stage_id", default="", help="Verify only this stage id.")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def browser_verify_cmd(run: str, stage_id: str, workdir: str, as_json: bool) -> None:
    """Replay RUN's browser activities offline and recompute every verdict.

    Reattaches each step's screenshot and DOM bytes from the content store,
    recomputes the anchor chain from genesis, and re-evaluates every recorded
    check against the reattached bytes -- so a tampered observation fails naming
    the exact step index, and a forged verdict fails naming the check id. Exit
    codes: 0 = verified, 1 = no run / no browser activity, 2 = mismatch (tamper).
    """
    from bernstein.core.orchestration.activity import ActivityKind
    from bernstein.core.orchestration.activity_modalities import (
        ContentStore,
        verify_run_activities,
    )

    root = Path(workdir).resolve()
    sdd_dir = root / ".sdd"
    cas_dir = sdd_dir / "cas"
    store = ContentStore(cas_dir) if cas_dir.exists() else None

    result = verify_run_activities(sdd_dir, run_id=run, store=store)
    stages = [
        s for s in result.stages if s.kind == ActivityKind.BROWSER.value and (not stage_id or s.stage_id == stage_id)
    ]

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "run": result.run_id,
                    "found": bool(stages),
                    "ok": bool(stages) and result.chain_ok and all(s.ok for s in stages),
                    "chain_ok": result.chain_ok,
                    "stages": [
                        {
                            "stage_id": s.stage_id,
                            "ok": s.ok,
                            "reason": s.reason,
                            "browser_verdict": _browser_verdict_payload(s.browser_verdict),
                        }
                        for s in stages
                    ],
                }
            )
        )
    else:
        console.print()
        console.print(f"[bold]Browser activity verify[/bold] run={result.run_id}")
        if not stages:
            console.print("[yellow]NO BROWSER ACTIVITY[/yellow] -- no anchored browser stage in this run.")
        for stage in stages:
            tag = "[green]OK[/green]" if stage.ok else "[red]MISMATCH[/red]"
            detail = "" if stage.ok else f" -- {stage.reason}"
            console.print(f"  {tag} {stage.stage_id}{detail}")
            verdict = stage.browser_verdict
            if verdict is None:
                continue
            for step in verdict.steps:
                step_tag = "[green]step OK[/green]" if step.ok else "[red]step FAIL[/red]"
                step_detail = f" -- {step.reason}" if not step.ok else ""
                console.print(f"    {step_tag} index={step.index}{step_detail}")
            for check in verdict.checks:
                check_tag = "[green]check OK[/green]" if check.ok else "[red]check FAIL[/red]"
                check_detail = f" -- {check.reason}" if not check.ok else ""
                console.print(f"    {check_tag} {check.check_id} (recomputed passed={check.passed}){check_detail}")

    if not stages:
        raise SystemExit(1)
    if not (result.chain_ok and all(s.ok for s in stages)):
        raise SystemExit(2)
    raise SystemExit(0)


__all__ = ["activity_group", "browser_group"]
