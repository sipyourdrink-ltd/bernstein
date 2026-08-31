"""``bernstein activity``: verify typed activity boundary crossings (#2311).

Bernstein's deterministic scheduler takes any agent modality -- research,
browser/computer-use, data, ops, coding -- behind one typed activity boundary,
anchoring each crossing into the run's canonical event journal as an
``activity.result`` entry that pins the ``evidence_set_hash`` (a pure function of
the content-addressed evidence the activity gathered).

``verify`` covers every anchored modality. On the dispatch side this group ships
one non-coding entry point, ``activity browser run``; research, data and ops
activities are constructed through the Python API today (see
``docs/operations/activity-boundary.md`` for the scope table).

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
import importlib
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


@activity_group.group("ops")
def ops_group() -> None:
    """Ops activities: deterministic change plans over signed input/output targets.

    The signed inputs are the target descriptors, the plan is the deterministic
    set of intended changes, and the signed outputs are the applied results.
    Exit codes: 0 = completed, 1 = no run / no activity, 2 = mismatch (tamper).
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
    "--driver",
    "driver_name",
    default=None,
    help="Select browser driver backend by registered name (e.g. browser_use, playwright).",
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
    driver_name: str | None,
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
        RECORDED_DRIVER_NAME,
        RECORDED_DRIVER_REFUSAL,
        BrowserDriver,
        BrowserDriverError,
        RecordedBrowserDriver,
        get_driver_factory,
    )
    from bernstein.core.orchestration.browser_worker import BrowserBudgetExceeded, BrowserWorker
    from bernstein.core.replay.journal import EventJournal

    root = Path(workdir).resolve()
    sdd_dir = root / ".sdd"
    flow_id, start_url, steps, final_checks, budget = _parse_flow(_load_json(Path(flow_path), label="flow document"))

    if recording_path is not None:
        if driver_name is not None:
            # Both name the backend. Silently letting --recording win would leave
            # an unknown --driver name unrefused, which AC1 requires refusing.
            raise click.BadParameter("--driver and --recording both select the backend; pass one or the other.")
        frames = _parse_recording(_load_json(Path(recording_path), label="recording"))

        def build(profile_dir: Path) -> BrowserDriver:
            return RecordedBrowserDriver(frames, profile_dir=profile_dir)
    else:
        dname = driver_name or "browser_use"
        try:
            factory = get_driver_factory(dname)
        except BrowserDriverError as exc:
            raise click.BadParameter(str(exc)) from exc
        if dname == RECORDED_DRIVER_NAME:
            # The recorded driver needs a tape, so it is not constructible from a
            # name. Refused here rather than at build time: a refusal raised
            # inside the worker is classified into a driver_error terminal state,
            # which never tells the operator to pass --recording.
            raise click.BadParameter(RECORDED_DRIVER_REFUSAL)

        def build(profile_dir: Path) -> BrowserDriver:
            return factory(profile_dir=profile_dir)

    worker = BrowserWorker(
        store=ContentStore(sdd_dir / "cas"),
        budget=budget,
        profile_root=sdd_dir / "browser-profiles",
    )
    try:
        run = worker.run(
            flow_id=flow_id,
            run_id=run_id,
            stage_id=stage_id,
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


# ---------------------------------------------------------------------------
# research activities
# ---------------------------------------------------------------------------


@activity_group.group("research")
def research_group() -> None:
    """Submit research activities: sourced reports with offline-resolvable citations.

    \b
    Examples:
      bernstein activity research run --input research.json --run run-42
      bernstein activity research run --input research.json --fetch-fn pkg.fetch --synthesise-fn pkg.synth --run run-42
    """


def _import_callable(ref: str) -> Any:
    """Resolve a dotted ``module:attribute`` reference to a callable.

    Args:
        ref: A dotted import path of the form ``package.module:attr``.

    Raises:
        click.BadParameter: When the reference is malformed or does not resolve
            to a callable.
    """
    if ":" not in ref:
        raise click.BadParameter(f"expected 'module:attribute', got {ref!r}")
    module_name, attr = ref.split(":", 1)
    if not module_name or not attr:
        raise click.BadParameter(f"expected 'module:attribute', got {ref!r}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise click.BadParameter(f"cannot import {module_name!r}: {exc}") from exc
    try:
        value = getattr(module, attr)
    except AttributeError as exc:
        raise click.BadParameter(f"{ref!r} did not resolve to an attribute") from exc
    if not callable(value):
        raise click.BadParameter(f"{ref!r} did not resolve to a callable")
    return value


@research_group.command("run")
@click.option(
    "--input",
    "input_path",
    type=click.Path(dir_okay=False, exists=True),
    required=True,
    help="Research input JSON document.",
)
@click.option("--run", "run_id", required=True, help="Run the activity anchors into.")
@click.option("--stage", "stage_id", default="research-0", show_default=True, help="Scheduler stage id.")
@click.option(
    "--fetch-fn",
    "fetch_fn_ref",
    default=None,
    help="Dotted 'module:attr' resolving to fetch_fn (default: synthetic).",
)
@click.option(
    "--synthesise-fn",
    "synthesise_ref",
    default=None,
    help="Dotted 'module:attr' resolving to synthesise (default: synthetic).",
)
@click.option(
    "--max-fetches",
    "max_fetches",
    type=int,
    default=16,
    show_default=True,
    help="Cost cap: max number of source fetches.",
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
def research_run_cmd(
    input_path: str,
    run_id: str,
    stage_id: str,
    fetch_fn_ref: str | None,
    synthesise_ref: str | None,
    max_fetches: int,
    workdir: str,
    as_json: bool,
) -> None:
    """Drive a research INPUT and anchor the result into RUN's journal.

    Each fetched page is content-addressed at fetch time and folded into a Merkle
    chain whose head is the run identity, then dispatched down the same path a
    coding spawn uses. A claim that cites a source not actually fetched is
    refused at the boundary before it reaches the journal. Exit codes:
    0 = completed with every claim carrying citations, 2 = completed with
    a failing claim, 3 = refused / failed / timed out.
    """
    from bernstein.core.orchestration.activity import ActivityRejected, TerminalState, dispatch_activity
    from bernstein.core.orchestration.activity_modalities import ContentStore
    from bernstein.core.orchestration.research_report import (
        ClaimVerdict,
        ResearchReportVerdict,
        verify_research_report,
    )
    from bernstein.core.orchestration.research_worker import (
        ClaimDraft,
        FetchedSource,
        ResearchBudget,
        ResearchBudgetExceeded,
        ResearchWorker,
        SpanRef,
    )
    from bernstein.core.replay.journal import EventJournal

    document = _load_json(Path(input_path), label="research input")
    raw_queries = document.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise click.BadParameter("research input must carry a non-empty queries list")

    fetch_fn: Any = None
    if fetch_fn_ref is not None:
        fetch_fn = _import_callable(fetch_fn_ref)
    synthesise: Any = None
    if synthesise_ref is not None:
        synthesise = _import_callable(synthesise_ref)

    queries: list[dict[str, str]] = []
    for position, row in enumerate(raw_queries):
        if not isinstance(row, dict):
            raise click.BadParameter(f"query {position}: must be an object")
        query = str(row.get("query", "")).strip()
        if not query:
            raise click.BadParameter(f"query {position}: must carry a non-empty 'query'")
        ref = str(row.get("ref", ""))
        queries.append({"query": query, "ref": ref})

    root = Path(workdir).resolve()
    sdd_dir = root / ".sdd"
    cas_dir = sdd_dir / "cas"
    store = ContentStore(cas_dir)

    def default_fetch(source_ref: str) -> bytes:
        # Synthesise deterministic stub content from the source ref so a run
        # without an injected fetch_fn still produces content-addressed
        # observations and a reproducible report.
        return f"synthetic research source: {source_ref}".encode()

    def default_synthesise(query: str, fetched: tuple[FetchedSource, ...]) -> list[ClaimDraft]:
        # Emit one claim per fetched source so the default mode still produces
        # a citation-lineage report, with the source ref as the quote so the
        # span is bound to the content hash the worker just recorded.
        drafts: list[ClaimDraft] = []
        for index, source in enumerate(fetched, start=1):
            drafts.append(
                ClaimDraft(
                    statement=f"Source {source.source_ref!r} supports query {query!r}.",
                    spans=(SpanRef(source_ref=source.source_ref, quote=source.source_ref),),
                    claim_id=f"c{index}",
                )
            )
        return drafts

    worker = ResearchWorker(
        store=store,
        budget=ResearchBudget(max_fetches=max_fetches),
    )

    last_outcome: tuple[Any, Any, list[Any]] | None = None
    terminal_state = TerminalState.COMPLETED
    reason_code = "ok"
    refused_queries: list[dict[str, str]] = []
    failed_queries: list[dict[str, str]] = []

    for position, query_row in enumerate(queries):
        query = query_row["query"]
        ref = query_row["ref"]
        candidate_sources = [ref] if ref else [f"query://{position}/{query}"]
        try:
            run = worker.run(
                query=query,
                sources=candidate_sources,
                fetch_fn=fetch_fn if fetch_fn is not None else default_fetch,
                synthesise=synthesise if synthesise is not None else default_synthesise,
                summary=query,
            )
        except ResearchBudgetExceeded as exc:
            refused_queries.append({"query": query, "ref": ref, "reason": str(exc)})
            terminal_state = TerminalState.REFUSED
            reason_code = "budget_exhausted"
            continue
        except ActivityRejected as exc:
            failed_queries.append({"query": query, "ref": ref, "reason": str(exc)})
            terminal_state = TerminalState.FAILED
            reason_code = "claim_refused"
            continue

        dispatch_activity(
            run.result,
            stage_id=f"{stage_id}-{position}" if len(queries) > 1 else stage_id,
            journal=EventJournal(run_id=run_id, sdd_dir=sdd_dir),
        )
        verdict: ResearchReportVerdict = verify_research_report(run.report, store=store)
        verdict_map = {cv.claim_id: cv.ok for cv in verdict.claims}
        failed_claims_list = [c.claim_id for c in run.report.claims if not verdict_map.get(c.claim_id, False)]
        if failed_claims_list:
            failed_claims_string = ",".join(str(c) for c in failed_claims_list)
            failed_queries.append({"query": query, "ref": ref, "claims": failed_claims_string})
            terminal_state = TerminalState.FAILED
            reason_code = "claim_failed"
        last_outcome = (run, verdict, failed_claims_list)

    if last_outcome is None and not refused_queries and not failed_queries:
        # No queries were processed at all -- treat as a refusal so the exit
        # code communicates the boundary never crossed.
        terminal_state = TerminalState.REFUSED
        reason_code = "no_queries"

    if as_json:
        payload: dict[str, Any] = {
            "run": run_id,
            "stage_id": stage_id,
            "terminal_state": terminal_state.value,
            "reason_code": reason_code,
            "queries": queries,
            "refused_queries": refused_queries,
            "failed_queries": failed_queries,
        }
        if last_outcome is not None:
            run, verdict, failed_claims = last_outcome
            payload.update(
                {
                    "query": run.plan.query,
                    "artifact_hash": run.result.artifact_hash,
                    "evidence_set_hash": run.result.evidence_set_hash,
                    "fetched": [{"source_ref": s.source_ref, "content_hash": s.content_hash} for s in run.fetched],
                    "claim_verdicts": [cv.to_dict() for cv in verdict.claims],
                    "failed_claims": failed_claims,
                }
            )
        console.print_json(json.dumps(payload))
    else:
        console.print()
        console.print(f"[bold]Research activity[/bold] run={run_id} stage={stage_id}")
        console.print(f"  terminal={terminal_state.value} reason={reason_code}")
        if last_outcome is not None:
            run, verdict, failed_claims = last_outcome
            console.print(
                f"  queries planned: {len(queries)}, sources fetched: {len(run.fetched)}, "
                f"claims drafted: {len(run.report.claims)}"
            )
            for cv in verdict.claims:
                cv_obj: ClaimVerdict = cv
                tag = "[green]cite OK[/green]" if cv_obj.ok else "[red]cite FAIL[/red]"
                detail = f" -- {cv_obj.reason}" if not cv_obj.ok and cv_obj.reason else ""
                console.print(f"    {tag} claim={cv_obj.claim_id} ({cv_obj.citations_checked} checked){detail}")
            for failed in failed_queries:
                console.print(f"  [red]FAIL[/red] query={failed.get('query')!r} claims={failed.get('claims', [])}")
            for refused in refused_queries:
                console.print(f"  [red]REFUSED[/red] query={refused.get('query')!r} -- {refused.get('reason')}")
        else:
            for refused in refused_queries:
                console.print(f"  [red]REFUSED[/red] query={refused.get('query')!r} -- {refused.get('reason')}")
            for failed in failed_queries:
                console.print(f"  [red]FAIL[/red] query={failed.get('query')!r} -- {failed.get('reason')}")

    if terminal_state is not TerminalState.COMPLETED:
        raise SystemExit(3)
    failed_claims_out: list[Any] = last_outcome[2] if last_outcome is not None else []
    raise SystemExit(2 if failed_claims_out else 0)


@ops_group.command("run")
@click.option(
    "--input",
    "input_path",
    type=click.Path(dir_okay=False, exists=True),
    required=True,
    help="Ops input JSON document.",
)
@click.option("--run", "run_id", required=True, help="Run the activity anchors into.")
@click.option("--stage", "stage_id", default="ops-0", show_default=True, help="Scheduler stage id.")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def ops_run_cmd(
    input_path: str,
    run_id: str,
    stage_id: str,
    workdir: str,
    as_json: bool,
) -> None:
    """Drive an ops INPUT and anchor the signed receipt into RUN's journal.

    Each input and output is content-addressed and Ed25519-signed with the
    install key. The deterministic plan is derived from the signed inputs,
    guaranteeing a replay recomputes the same plan hash from the same inputs.
    Exit codes: 0 = completed, 2 = completed with a failing side effect.
    """
    from bernstein.core.orchestration.activity import dispatch_activity
    from bernstein.core.orchestration.activity_modalities import ContentStore, OpsActivity, verify_data_ops_receipt
    from bernstein.core.replay.journal import EventJournal

    document = _load_json(Path(input_path), label="ops input")

    root = Path(workdir).resolve()
    sdd_dir = root / ".sdd"
    cas_dir = sdd_dir / "cas"
    store = ContentStore(cas_dir)

    inputs = document.get("inputs", [])
    if not isinstance(inputs, list):
        raise click.BadParameter("ops input must carry a non-empty inputs list")

    # Load keys from document
    private_key_pem = document.get("private_key_pem", "")
    public_key_pem = document.get("public_key_pem", "")
    if not private_key_pem or not public_key_pem:
        raise click.BadParameter("ops input must carry both private_key_pem and public_key_pem")

    # Create activity with keys
    activity = OpsActivity(
        store=store,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
    )

    # Add inputs
    for position, row in enumerate(inputs):
        if not isinstance(row, dict):
            raise click.BadParameter(f"input {position}: must be an object")
        ref = str(row.get("ref", "")).strip()
        content_b64 = str(row.get("content_b64", "")).strip()
        if not ref:
            raise click.BadParameter(f"input {position}: must carry a non-empty 'ref'")
        if not content_b64:
            raise click.BadParameter(f"input {position}: must carry a non-empty 'content_b64'")

        import base64

        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise click.BadParameter(f"input {position}: content_b64 is not valid base64: {exc}") from exc

        activity.add_input(ref=ref, content=content)

    # Create plan
    steps = document.get("steps", [])
    if not isinstance(steps, list):
        raise click.BadParameter("ops input steps must be a list")
    activity.plan(steps)

    # Add outputs from document
    outputs = document.get("outputs", [])
    if isinstance(outputs, list):
        for position, row in enumerate(outputs):
            if not isinstance(row, dict):
                raise click.BadParameter(f"output {position}: must be an object")
            ref = str(row.get("ref", "")).strip()
            content_b64 = str(row.get("content_b64", "")).strip()
            if not ref:
                raise click.BadParameter(f"output {position}: must carry a non-empty 'ref'")
            if not content_b64:
                raise click.BadParameter(f"output {position}: must carry a non-empty 'content_b64'")

            import base64

            try:
                content = base64.b64decode(content_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise click.BadParameter(f"output {position}: content_b64 is not valid base64: {exc}") from exc

            activity.add_output(ref=ref, content=content)

    result = activity.finish()
    from bernstein.core.orchestration.activity_modalities import DataOpsReceipt

    verdict = verify_data_ops_receipt(DataOpsReceipt.from_dict(result.artifact), store=store)

    dispatch_activity(result, stage_id=stage_id, journal=EventJournal(run_id=run_id, sdd_dir=sdd_dir))

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "run": run_id,
                    "stage_id": stage_id,
                    "terminal_state": result.terminal_state.value,
                    "reason_code": result.reason_code,
                    "artifact_hash": result.artifact_hash,
                    "evidence_set_hash": result.evidence_set_hash,
                    "verified": verdict.ok,
                    "signatures_ok": verdict.signatures_ok,
                    "plan_ok": verdict.plan_ok,
                    "inputs": [{"ref": i.ref, "content_hash": i.content_hash} for i in activity._inputs],
                    "outputs": [{"ref": o.ref, "content_hash": o.content_hash} for o in activity._outputs],
                }
            )
        )
    else:
        console.print()
        console.print(f"[bold]Ops activity[/bold] run={run_id} stage={stage_id}")
        console.print(f"  terminal={result.terminal_state.value} reason={result.reason_code}")
        console.print(f"  artifact hash: {result.artifact_hash}")
        console.print(f"  inputs: {len(activity._inputs)}, outputs: {len(activity._outputs)}")
        if verdict.ok:
            console.print("[green]completed[/green] -- plan and signatures verified.")

    raise SystemExit(0 if verdict.ok else 2)


@ops_group.command("verify")
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
def ops_verify_cmd(run: str, stage_id: str, workdir: str, as_json: bool) -> None:
    """Replay RUN's ops activities offline and recompute every verdict.

    Reattaches each signed input/output evidence bytes from the content store,
    recomputes the anchor chain from genesis, and re-evaluates the signed receipt
    and signatures -- so a tampered input fails naming the ref and a forged
    receipt fails signature verification. Exit codes: 0 = verified, 1 = no run /
    no ops activity, 2 = mismatch (tamper).
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
    stages = [s for s in result.stages if s.kind == ActivityKind.OPS.value and (not stage_id or s.stage_id == stage_id)]

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
                            "evidence_reattached": s.evidence_reattached,
                            "signed_receipt_verified": s.signed_receipt_verified,
                        }
                        for s in stages
                    ],
                }
            )
        )
    else:
        console.print()
        console.print(f"[bold]Ops activity verify[/bold] run={result.run_id}")
        if not stages:
            console.print("[yellow]NO OPS ACTIVITY[/yellow] -- no anchored ops stage in this run.")
        for stage in stages:
            tag = "[green]OK[/green]" if stage.ok else "[red]MISMATCH[/red]"
            detail = "" if stage.ok else f" -- {stage.reason}"
            console.print(f"  {tag} {stage.stage_id}{detail}")
            if stage.evidence_reattached:
                console.print(f"    evidence reattached: {stage.stage_id}")
            if stage.signed_receipt_verified:
                console.print(f"    signed receipt verified: {stage.stage_id}")

    if not stages:
        raise SystemExit(1)
    if not (result.chain_ok and all(s.ok for s in stages)):
        raise SystemExit(2)
    raise SystemExit(0)


# ---------------------------------------------------------------------------
# data activities
# ---------------------------------------------------------------------------


@activity_group.group("data")
def data_group() -> None:
    """Submit and inspect data activities: deterministic transform plans over signed I/O.

    \\b
    Examples:
      bernstein activity data run --input input.json --run run-42 --stage data-0
      bernstein activity data verify run-42 --stage data-0
    """


@data_group.command("run")
@click.option(
    "--input",
    "input_path",
    type=click.Path(dir_okay=False, exists=True),
    required=True,
    help="Data input JSON document.",
)
@click.option("--run", "run_id", required=True, help="Run the activity anchors into.")
@click.option("--stage", "stage_id", default="data-0", show_default=True, help="Scheduler stage id.")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def data_run_cmd(
    input_path: str,
    run_id: str,
    stage_id: str,
    workdir: str,
    as_json: bool,
) -> None:
    """Drive a data INPUT and anchor the signed receipt into RUN's journal.

    Each input and output is content-addressed and Ed25519-signed with the
    install key. The deterministic plan is derived from the signed inputs,
    guaranteeing a replay recomputes the same plan hash from the same inputs.
    Exit codes: 0 = completed, 2 = completed with a failing side effect.
    """
    from bernstein.core.orchestration.activity import dispatch_activity
    from bernstein.core.orchestration.activity_modalities import ContentStore, DataActivity, verify_data_ops_receipt
    from bernstein.core.replay.journal import EventJournal

    document = _load_json(Path(input_path), label="data input")

    root = Path(workdir).resolve()
    sdd_dir = root / ".sdd"
    cas_dir = sdd_dir / "cas"
    store = ContentStore(cas_dir)

    inputs = document.get("inputs", [])
    if not isinstance(inputs, list):
        raise click.BadParameter("data input must carry a non-empty inputs list")

    private_key_pem = document.get("private_key_pem", "")
    public_key_pem = document.get("public_key_pem", "")
    if not private_key_pem or not public_key_pem:
        raise click.BadParameter("data input must carry both private_key_pem and public_key_pem")

    activity = DataActivity(
        store=store,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
    )

    for position, row in enumerate(inputs):
        if not isinstance(row, dict):
            raise click.BadParameter(f"input {position}: must be an object")
        ref = str(row.get("ref", "")).strip()
        content_b64 = str(row.get("content_b64", "")).strip()
        if not ref:
            raise click.BadParameter(f"input {position}: must carry a non-empty 'ref'")
        if not content_b64:
            raise click.BadParameter(f"input {position}: must carry a non-empty 'content_b64'")

        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise click.BadParameter(f"input {position}: content_b64 is not valid base64: {exc}") from exc

        activity.add_input(ref=ref, content=content)

    steps = document.get("steps", [])
    if not isinstance(steps, list):
        raise click.BadParameter("data input steps must be a list")
    activity.plan(steps)

    outputs = document.get("outputs", [])
    if isinstance(outputs, list):
        for position, row in enumerate(outputs):
            if not isinstance(row, dict):
                raise click.BadParameter(f"output {position}: must be an object")
            ref = str(row.get("ref", "")).strip()
            content_b64 = str(row.get("content_b64", "")).strip()
            if not ref:
                raise click.BadParameter(f"output {position}: must carry a non-empty 'ref'")
            if not content_b64:
                raise click.BadParameter(f"output {position}: must carry a non-empty 'content_b64'")

            try:
                content = base64.b64decode(content_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise click.BadParameter(f"output {position}: content_b64 is not valid base64: {exc}") from exc

            activity.add_output(ref=ref, content=content)

    result = activity.finish()
    from bernstein.core.orchestration.activity_modalities import DataOpsReceipt

    verdict = verify_data_ops_receipt(DataOpsReceipt.from_dict(result.artifact), store=store)

    dispatch_activity(result, stage_id=stage_id, journal=EventJournal(run_id=run_id, sdd_dir=sdd_dir))

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "run": run_id,
                    "stage_id": stage_id,
                    "terminal_state": result.terminal_state.value,
                    "reason_code": result.reason_code,
                    "artifact_hash": result.artifact_hash,
                    "evidence_set_hash": result.evidence_set_hash,
                    "verified": verdict.ok,
                    "signatures_ok": verdict.signatures_ok,
                    "plan_ok": verdict.plan_ok,
                    "inputs": [{"ref": i.ref, "content_hash": i.content_hash} for i in activity._inputs],
                    "outputs": [{"ref": o.ref, "content_hash": o.content_hash} for o in activity._outputs],
                }
            )
        )
    else:
        console.print()
        console.print(f"[bold]Data activity[/bold] run={run_id} stage={stage_id}")
        console.print(f"  terminal={result.terminal_state.value} reason={result.reason_code}")
        console.print(f"  artifact hash: {result.artifact_hash}")
        console.print(f"  inputs: {len(activity._inputs)}, outputs: {len(activity._outputs)}")
        if verdict.ok:
            console.print("[green]completed[/green] -- plan and signatures verified.")

    raise SystemExit(0 if verdict.ok else 2)


@data_group.command("verify")
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
def data_verify_cmd(run: str, stage_id: str, workdir: str, as_json: bool) -> None:
    """Replay RUN's data activities offline and recompute every verdict.

    Reattaches each signed input/output evidence bytes from the content store,
    recomputes the anchor chain from genesis, and re-evaluates the signed receipt
    and signatures -- so a tampered input fails naming the ref and a forged
    receipt fails signature verification. Exit codes: 0 = verified, 1 = no run /
    no data activity, 2 = mismatch (tamper).
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
        s for s in result.stages if s.kind == ActivityKind.DATA.value and (not stage_id or s.stage_id == stage_id)
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
                            "evidence_reattached": s.evidence_reattached,
                            "signed_receipt_verified": s.signed_receipt_verified,
                        }
                        for s in stages
                    ],
                }
            )
        )
    else:
        console.print()
        console.print(f"[bold]Data activity verify[/bold] run={result.run_id}")
        if not stages:
            console.print("[yellow]NO DATA ACTIVITY[/yellow] -- no anchored data stage in this run.")
        for stage in stages:
            tag = "[green]OK[/green]" if stage.ok else "[red]MISMATCH[/red]"
            detail = "" if stage.ok else f" -- {stage.reason}"
            console.print(f"  {tag} {stage.stage_id}{detail}")
            if stage.evidence_reattached:
                console.print(f"    evidence reattached: {stage.stage_id}")
            if stage.signed_receipt_verified:
                console.print(f"    signed receipt verified: {stage.stage_id}")

    if not stages:
        raise SystemExit(1)
    if not (result.chain_ok and all(s.ok for s in stages)):
        raise SystemExit(2)
    raise SystemExit(0)


__all__ = ["activity_group", "browser_group", "data_group", "ops_group", "research_group"]
