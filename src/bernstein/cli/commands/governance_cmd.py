"""``bernstein govern``: verify RBAC + budget decisions as chain projections.

Issue #2309. Recomputes every access and budget decision recorded for a run from
the signed lineage spine and confirms the recorded verdicts:

    bernstein govern verify <run> --bindings <file> [--ledger <file>]

Access decisions re-resolve the subject's role from the presented signed role
bindings and re-project the role's permissions onto the action. Budget decisions
recompute per-subject spend from the cost ledger (never a stored counter) and
re-derive the verdict. A tampered verdict, a widened permission binding, or a
diverged ledger fails the check.

    bernstein govern plan --playbook <file> --inventory <file> [--workdir <path>]

Generate a signed, lineage-bearing govern plan representing the diff between
declared posture (playbook) and enumerated environment (inventory). The plan
contains one entry per mismatch (FORBIDDEN, ABSENT, WIDER_CEILING, UNKNOWN)
and is anchored in the lineage spine for offline verification.

    bernstein govern plan ... --remediation-plan <file>

Collect the remedies the playbook declares for those findings into one unsigned
proposal, anchored the same way. A finding whose clause declares no remedy is
listed in the proposal as unremediated rather than dropped.

    bernstein govern ingest --spans <file|-> --source <label> [--profile <name>]

Anchor OTLP spans reported by a runtime Bernstein did not schedule (#4962).
The record starts at ``Orchestrator.run()``, so activity driven elsewhere
produces no chain events and no receipt can mention it. This is the first
transport into the ingest boundary: a file or stdin. A payload the boundary
rejects appends nothing, and a submission already anchored returns the receipt
it was anchored with instead of a second one.

    bernstein govern posture [--workdir <path>] [--json-output]

Score the install's posture from chain-evidenced facts only. The number is a
projection of the lineage log; no configuration file is read, so switching a
control on cannot move it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

import click
from rich.console import Console
from rich.table import Table

from bernstein.cli.commands.govern_cmd import govern_inventory_cmd, govern_reconcile_cmd
from bernstein.cli.helpers import console
from bernstein.core.govern import collect_remediation as _collect_remediation
from bernstein.core.govern import compute_plan as _compute_plan
from bernstein.core.govern.audit_sweep import CheckVerdict
from bernstein.core.govern.compliance_checks import (
    CMP_AREA,
    ComplianceCheckSpec,
    ComplianceFramework,
    count_by_outcome,
    iter_compliance_checks,
    required_check_ids,
    run_compliance_checks,
    select_check_ids,
)
from bernstein.core.lineage.spine import LineageSpine

if TYPE_CHECKING:
    from bernstein.core.govern.plan_models import GovernPlan


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


@click.group("govern")
def govern_group() -> None:
    """Verify RBAC and budget decisions as projections over the audit chain.

    \b
      bernstein govern verify <run> --bindings b.json --ledger ledger.jsonl
      bernstein govern plan --playbook p.json --inventory i.json [--workdir w]
      bernstein govern ingest --spans spans.json --source otel-collector-prod
      bernstein govern posture [--workdir w] [--json-output]
      bernstein govern inventory --render mermaid|dot --store PATH
      bernstein govern audit-compliance [--workdir .] [--only CMP] [--skip ID] [--profile soc2]
      bernstein govern audit-keys
    """


@govern_group.command("verify")
@click.argument("run_id", required=True)
@click.option(
    "--bindings",
    "bindings_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Signed role-bindings JSON the access decisions project over.",
)
@click.option(
    "--ledger",
    "ledger_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Spend ledger JSONL for recomputing budget decisions (required when the run has budget rows).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def governance_verify_cmd(run_id: str, bindings_file: str, ledger_file: str | None, workdir: str) -> None:
    """Recompute every access and budget verdict for *run_id* and match them.

    Exit codes: 0 = verified, 1 = no records / bad input, 2 = mismatch.
    """
    from bernstein.core.security.governance import RoleBindings, verify_governance

    root = Path(workdir).resolve()
    bindings = RoleBindings.from_dict(json.loads(Path(bindings_file).read_text(encoding="utf-8")))
    ledger_path = Path(ledger_file).resolve() if ledger_file else None

    result = verify_governance(
        run_id=run_id,
        lineage_root=_lineage_root(root),
        hmac_key=_load_hmac_key(),
        bindings=bindings,
        ledger_path=ledger_path,
    )

    console.print()
    console.print(f"[bold]Governance verify[/bold] run={run_id}")
    console.print(f"  decisions checked  {result.checked}")
    if result.ok:
        console.print("[green]OK[/green] -- every access and budget verdict recomputes from the chain.")
        raise SystemExit(0)
    if result.checked == 0:
        console.print(f"[yellow]NO RECORDS[/yellow] -- {result.reason}")
        raise SystemExit(1)
    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)


@govern_group.command("plan")
@click.option(
    "--playbook",
    "playbook_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSON file describing the declared posture (see compute_plan schema).",
)
@click.option(
    "--inventory",
    "inventory_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSON file describing the enumerated environment (see compute_plan schema).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--remediation-plan",
    "remediation_out",
    type=click.Path(dir_okay=False),
    default=None,
    help="Collect the remedies the playbook declares for these findings into one unsigned proposal here.",
)
def governance_plan_cmd(
    playbook_file: str,
    inventory_file: str,
    workdir: str,
    remediation_out: str | None,
) -> None:
    """Generate a signed, lineage-bearing govern plan.

    Exit 0 always (a signed empty plan is valid).
    """
    root = Path(workdir).resolve()

    playbook = json.loads(Path(playbook_file).read_text(encoding="utf-8"))
    inventory = json.loads(Path(inventory_file).read_text(encoding="utf-8"))

    timestamp = int(time.time())

    plan = _compute_plan(
        playbook=playbook,
        inventory=inventory,
        run_id="govern-plan",
        timestamp=timestamp,
    )

    # Anchoring in the lineage spine
    from bernstein.core.lineage.spine import LineageSpine

    hmac_key = _load_hmac_key()
    lineage_root = _lineage_root(root)
    spine = LineageSpine(lineage_root, run_id="govern-plan", hmac_key=hmac_key)

    # Write the plan to the lineage spine
    # We use the plan's canonical bytes as the content to anchor
    artifact_path = "governance-plan.json"
    anchor_hash = spine.record(
        artifact_path=artifact_path,
        content=plan.to_canonical_bytes(),
        actor="bernstein.govern",
        step_id=plan.inputs_hash,
        model="none",
        timestamp=timestamp,
    )

    # Also persist the plan JSON to a file in the governance decisions dir
    decisions_dir = lineage_root / "govern-plan"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    plan_path = decisions_dir / "plan.json"
    plan_path.write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Print a Rich table summarizing the plan
    console_obj = Console()
    console_obj.print()
    console_obj.print("[bold]Governance plan[/bold] run=govern-plan")
    console_obj.print(f"  Plan entries: {len(plan.entries)}")
    console_obj.print(f"  Inputs hash: {plan.inputs_hash}")
    console_obj.print(f"  Timestamp: {timestamp}")
    console_obj.print(f"  Journal anchor: {anchor_hash}")

    table = Table(title="Plan Entries", show_header=True, header_style="bold magenta")
    table.add_column("Kind")
    table.add_column("Surface")
    table.add_column("Playbook Clause")
    table.add_column("Observed Value")
    table.add_column("Declared Value")
    table.add_column("Evidence Ref")

    for entry in plan.entries:
        table.add_row(
            entry.kind.value,
            entry.surface,
            entry.playbook_clause,
            str(entry.observed_value) if entry.observed_value is not None else "",
            str(entry.declared_value) if entry.declared_value is not None else "",
            entry.evidence_ref,
        )

    console_obj.print(table)

    if remediation_out is not None:
        _write_remediation_proposal(
            plan=plan,
            playbook=playbook,
            timestamp=timestamp,
            out_path=Path(remediation_out),
            spine=spine,
            console_obj=console_obj,
        )

    raise SystemExit(0)


def _write_remediation_proposal(
    *,
    plan: GovernPlan,
    playbook: dict[str, object],
    timestamp: int,
    out_path: Path,
    spine: LineageSpine,
    console_obj: Console,
) -> None:
    """Collect the declared remedies, anchor the proposal, and write it out."""
    proposal = _collect_remediation(plan=plan, playbook=playbook, timestamp=timestamp)

    spine.record(
        artifact_path=f"govern-plan/remediation-{proposal.content_hash()[:16]}.json",
        content=proposal.to_canonical_bytes(),
        actor="bernstein.govern.plan",
        step_id=proposal.plan_hash,
        model="none",
        timestamp=timestamp,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    console_obj.print()
    console_obj.print("[bold]Remediation proposal[/bold] (unsigned draft)")
    console_obj.print(f"  Steps: {len(proposal.steps)}")
    console_obj.print(f"  Without a declared remedy: {len(proposal.unremediated)} finding(s)")
    for finding in proposal.unremediated:
        console_obj.print(f"    [yellow]{finding.finding_kind}[/yellow] {finding.surface} -- {finding.reason}")
    console_obj.print(f"  Proposal hash: {proposal.content_hash()}")
    console_obj.print(f"  Proposal: {out_path}")
    console_obj.print("[dim]Not applied: sign the proposal before anything executes it.[/dim]")


@govern_group.command("posture")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--json-output",
    "as_json",
    is_flag=True,
    help="Print the signed canonical document instead of a table.",
)
def governance_posture_cmd(workdir: str, as_json: bool) -> None:
    """Score this install's posture from chain-evidenced facts only.

    The score consumes the per-control coverage report over the lineage log and
    reads no configuration, so enabling a control cannot raise it; producing
    evidence for that control can. The document names every contributing chain
    event, the weights version, and its own denominator -- the weight that was
    measurable, not the weight that exists.

    Exit 0 always. A score is a measurement, not a gate.
    """
    from bernstein.core.security.security_posture import (
        collect_evidenced_posture,
        evidenced_posture_json,
        format_evidenced_posture,
    )

    root = Path(workdir).resolve()

    if as_json:
        click.echo(evidenced_posture_json(root, hmac_key=_load_hmac_key()))
        raise SystemExit(0)

    console.print()
    console.print(format_evidenced_posture(collect_evidenced_posture(root)))
    raise SystemExit(0)


def _connector_configured() -> bool:
    """Return True when a connector (LLM provider) is configured.

    Checks the bernstein.yaml seed for a non-\"none\" internal_llm_provider.
    Gracefully returns False when no seed file exists.
    """
    from bernstein.cli.helpers import find_seed_file

    seed_path = find_seed_file()
    if seed_path is None:
        return False
    try:
        from bernstein.core.config.seed import parse_seed

        seed = parse_seed(seed_path)
        return seed.internal_llm_provider not in ("none", "")
    except Exception:
        return False


def _run_inventory_discovery(workdir: Path) -> dict[str, object]:
    """Run the inventory discovery pass.

    Performs environment enumeration and returns the inventory dict.
    Raises on failure.
    """
    import logging

    logger = logging.getLogger(__name__)

    surfaces: list[dict[str, str]] = []

    try:
        surfaces.extend(_discover_git_remotes(workdir))
    except Exception as exc:
        logger.debug("Git remote discovery skipped: %s", exc)

    try:
        surfaces.extend(_discover_environment_variables())
    except Exception as exc:
        logger.debug("Env-var discovery skipped: %s", exc)

    try:
        surfaces.extend(_discover_file_permissions(workdir))
    except Exception as exc:
        logger.debug("File-permission discovery skipped: %s", exc)

    return {"surfaces": surfaces}


def _discover_git_remotes(workdir: Path) -> list[dict[str, str]]:
    """Enumerate git remote URLs as governed surfaces."""
    import subprocess

    surfaces: list[dict[str, str]] = []
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                remote_name = parts[0]
                url = parts[1]
                surfaces.append(
                    {
                        "surface": f"git:remote:{remote_name}",
                        "observed_value": url,
                        "evidence_ref": f"git-remote:{remote_name}",
                    }
                )
    except Exception:
        pass
    return surfaces


def _discover_environment_variables() -> list[dict[str, str]]:
    """Enumerate relevant environment variables as governed surfaces."""
    import os

    governed_prefixes = ("BERNSTEIN_", "OPENAI_", "OPENROUTER_", "GITHUB_", "AWS_", "AZURE_", "GCP_")
    surfaces: list[dict[str, str]] = []
    for key, value in os.environ.items():
        for prefix in governed_prefixes:
            if key.startswith(prefix):
                surfaces.append(
                    {
                        "surface": f"env:{key}",
                        "observed_value": "set" if value else "",
                        "evidence_ref": f"env-var:{key}",
                    }
                )
                break
    return surfaces


def _discover_file_permissions(workdir: Path) -> list[dict[str, str]]:
    """Enumerate sensitive file permissions as governed surfaces."""
    import stat

    governed_paths = [".env", ".env.local", ".sdd/audit.key", "bernstein.yaml"]
    surfaces: list[dict[str, str]] = []
    for rel_path in governed_paths:
        full_path = workdir / rel_path
        if full_path.exists():
            try:
                st = full_path.stat()
                mode = stat.filemode(st.st_mode)
                surfaces.append(
                    {
                        "surface": f"file:{rel_path}",
                        "observed_value": mode,
                        "evidence_ref": f"file-perms:{rel_path}",
                    }
                )
            except Exception:
                pass
    return surfaces


def _build_findings_from_inventory(
    inventory: dict[str, object],
    inventory_hash: str,
    timestamp: int,
) -> dict[str, object]:
    """Build a FindingsDocument from an inventory dict."""
    from bernstein.core.govern import Finding, FindingsDocument

    findings: list[Finding] = []
    raw_surfaces = cast("list[dict[str, Any]]", inventory.get("surfaces", []))
    for raw_surface in raw_surfaces:
        surface_id = str(raw_surface.get("surface", ""))
        observed_value = str(raw_surface.get("observed_value", ""))
        evidence_ref = str(raw_surface.get("evidence_ref", ""))
        finding = Finding(
            surface=surface_id,
            observed_value=observed_value,
            evidence_ref=evidence_ref,
            readable=bool(observed_value),
        )
        findings.append(finding)

    doc = FindingsDocument(
        findings=tuple(findings),
        inventory_hash=inventory_hash,
        timestamp=timestamp,
    )
    return doc.to_dict()


def _build_playbook_prompt(findings_dict: dict[str, object], seed: str | None) -> str:
    """Build the prompt sent to the model from the findings document."""
    findings_lines: list[str] = []
    raw_findings = cast("list[dict[str, Any]]", findings_dict.get("findings", []))
    for f in raw_findings:
        readable_str = "readable" if f.get("readable") else "UNREADABLE"
        findings_lines.append(
            f"  - surface: {f['surface']}\n"
            f"    observed: {f.get('observed_value', '')}\n"
            f"    status: {readable_str}\n"
            f"    evidence: {f.get('evidence_ref', '')}"
        )

    findings_text = "\n".join(findings_lines) if findings_lines else "  (no surfaces enumerated)"

    prompt = (
        "You are a security governance assistant. Based on the following enumerated environment findings, "
        "draft a governance playbook in JSON format.\n\n"
        "Findings:\n"
        f"{findings_text}\n\n"
        "Respond ONLY with a valid JSON object with this exact schema:\n"
        "{\n"
        '  "forbidden": [{"surface": "...", "clause": "..."}],\n'
        '  "required": [{"surface": "...", "clause": "...", "declared_value": "..."}],\n'
        '  "permitted": [{"surface": "...", "clause": "...", "declared_ceiling": "..."}]\n'
        "}\n\n"
        "Rules:\n"
        "- Surfaces marked UNREADABLE cannot be declared compliant; put them in the appropriate list with a clause.\n"
        "- Surfaces marked readable should be evaluated for risk and included as appropriate.\n"
        "- Keep clauses concise and actionable.\n"
    )

    if seed:
        prompt += f"\n\nSeed for reproducibility: {seed}"

    return prompt


def _parse_playbook_json(raw_output: str) -> dict[str, object]:
    """Parse model output as playbook JSON, stripping markdown code fences if present."""
    text = raw_output.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model output is not valid JSON: {exc}") from exc

    if not isinstance(result, dict):
        raise ValueError(f"Model output must be a JSON object, got {type(result).__name__}")

    return result


@govern_group.command("discover")
@click.option(
    "--inventory",
    "inventory_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Path to inventory JSON file. If omitted, runs the discovery pass.",
)
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(".sdd/govern"),
    help="Directory to write findings.json and proposal.json.",
)
@click.option(
    "--seed",
    "seed_value",
    type=str,
    default=None,
    help="Optional seed string for reproducibility.",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def govern_discover_cmd(
    inventory_file: str | None,
    output_dir: Path,
    seed_value: str | None,
    workdir: str,
) -> None:
    """Run governance discovery and optionally draft a playbook.

    Runs the environment inventory pass (or reads an existing inventory file),
    produces a FindingsDocument, and — when a connector is configured — calls
    the operator's model to draft a playbook proposal.

    Exit 0 always. A malformed model response exits 1.

    Examples::

        bernstein govern discover
        bernstein govern discover --inventory inventory.json
        bernstein govern discover --output-dir .sdd/govern --seed my-seed
    """
    from bernstein.core.govern import DraftProposal, FindingsDocument, ProposalStatus

    root = Path(workdir).resolve()
    output_dir_abs = output_dir.resolve() if not output_dir.is_absolute() else output_dir

    timestamp = int(time.time())

    if inventory_file:
        inventory_raw = json.loads(Path(inventory_file).read_text(encoding="utf-8"))
    else:
        inventory_raw = _run_inventory_discovery(root)

    inventory_bytes = json.dumps(
        inventory_raw,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    inventory_hash = "sha256:" + hashlib.sha256(inventory_bytes).hexdigest()

    findings_dict = _build_findings_from_inventory(inventory_raw, inventory_hash, timestamp)

    findings_doc = FindingsDocument.from_dict(findings_dict)
    findings_hash = findings_doc.content_hash()

    output_dir_abs.mkdir(parents=True, exist_ok=True)

    findings_path = output_dir_abs / f"findings-{findings_hash[:16]}.json"
    findings_path.write_text(
        json.dumps(findings_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    console.print()
    console.print("[bold]Governance discover[/bold]")
    finding_rows = cast("list[dict[str, Any]]", findings_dict.get("findings", []))
    console.print(f"  Findings: {len(finding_rows)} surfaces")
    console.print(f"  Inventory hash: {inventory_hash}")
    console.print(f"  Findings hash: {findings_hash}")
    rel_path = findings_path.relative_to(root) if findings_path.is_relative_to(root) else findings_path
    console.print(f"  Findings: {rel_path}")

    if not _connector_configured():
        console.print("[dim]No connector configured — emitting findings only.[/dim]")
        raise SystemExit(0)

    seed = seed_value or ""

    prompt = _build_playbook_prompt(findings_dict, seed)

    try:
        from bernstein.cli.helpers import find_seed_file
        from bernstein.core.config.seed import parse_seed

        seed_path = find_seed_file()
        seed_config = parse_seed(seed_path) if seed_path else None
        model = seed_config.internal_llm_model if seed_config else "nvidia/nemotron-3-super-120b-a12b"
        provider = seed_config.internal_llm_provider if seed_config else "openrouter_free"
    except Exception:
        model = "nvidia/nemotron-3-super-120b-a12b"
        provider = "openrouter_free"

    from bernstein.core.llm import call_llm

    console.print(f"  Model: {model} ({provider})")

    try:
        raw_output = asyncio.run(
            call_llm(
                prompt=prompt,
                model=model,
                provider=provider,
                max_tokens=4000,
                temperature=0.7,
            )
        )
    except RuntimeError as exc:
        console.print(f"[red]LLM call failed:[/red] {exc}")
        raise SystemExit(1) from exc

    try:
        playbook = _parse_playbook_json(raw_output)
    except ValueError as exc:
        console.print(f"[red]Model output parse failed:[/red] {exc}")
        raise SystemExit(1) from exc

    proposal = DraftProposal(
        findings_hash=findings_hash,
        prompt=prompt,
        playbook=playbook,
        model=model,
        timestamp=timestamp,
        status=ProposalStatus.DRAFT,
        human_signature=None,
    )

    lineage_root = _lineage_root(root)
    lineage_root.mkdir(parents=True, exist_ok=True)

    hmac_key = _load_hmac_key()
    spine = LineageSpine(lineage_root, run_id="govern-discover", hmac_key=hmac_key)

    artifact_path = f"govern-discover/proposal-{proposal.content_hash()[:16]}.json"
    spine.record(
        artifact_path=artifact_path,
        content=proposal.to_canonical_bytes(),
        actor="bernstein.govern.discover",
        step_id=findings_hash,
        model=model,
        timestamp=timestamp,
    )

    proposal_path = output_dir_abs / "proposal.json"
    proposal_path.write_text(
        json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    rel_proposal_path = proposal_path.relative_to(root) if proposal_path.is_relative_to(root) else proposal_path
    console.print(f"  Proposal: {rel_proposal_path}")

    raise SystemExit(0)


@govern_group.command("ingest")
@click.option(
    "--spans",
    "spans_file",
    required=True,
    type=click.Path(dir_okay=False, allow_dash=True),
    help="OTLP/JSON span file, or - to read the payload from stdin.",
)
@click.option(
    "--source",
    "source_label",
    required=True,
    help="Identity of the reporting source (e.g. otel-collector-prod). Bound into the receipt.",
)
@click.option(
    "--profile",
    "profile_name",
    default="generic",
    show_default=True,
    help="Ingest profile driving the OTLP attribute mapping.",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the receipt as JSON and nothing else.")
def governance_ingest_cmd(
    spans_file: str,
    source_label: str,
    profile_name: str,
    workdir: str,
    as_json: bool,
) -> None:
    """Anchor OTLP spans reported by a runtime Bernstein did not schedule.

    Reads OTLP/JSON spans from a file or stdin, records them in the audit
    chain, and prints the signed receipt covering the submission. The receipt
    states its own coverage: the activity was reported, not scheduled here.

    Exit codes: 0 = anchored, 1 = payload rejected (nothing was appended).
    """
    import sys

    from bernstein.core.observability.ingest_profiles import ProfileNotFound, get_profile
    from bernstein.core.observability.otlp_ingest import OTLPIngestAdapter, OTLPIngestError
    from bernstein.core.observability.otlp_ingest_receipt import IngestOTLPReceipt

    def _reject(reason: str) -> NoReturn:
        console.print(f"[red]REJECTED[/red] -- {reason}")
        raise SystemExit(1)

    root = Path(workdir).resolve()

    if spans_file == "-":
        raw = sys.stdin.read()
    else:
        path = Path(spans_file)
        if not path.is_file():
            _reject(f"{spans_file} is not a file")
        raw = path.read_text(encoding="utf-8")

    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        _reject(f"payload is not valid JSON: {exc}")

    if isinstance(payload, dict):
        spans: list[dict[str, Any]] = [cast("dict[str, Any]", payload)]
    elif isinstance(payload, list):
        spans = cast("list[dict[str, Any]]", payload)
    else:
        _reject(f"payload must be a span object or a list of them, got {type(payload).__name__}")

    try:
        get_profile(profile_name)
    except ProfileNotFound:
        _reject(f"unknown ingest profile {profile_name!r}")

    # Validate before anchoring: a payload the boundary would reject must not
    # leave a partial record behind, so parsing happens ahead of every append.
    adapter = OTLPIngestAdapter(source_label=source_label)
    try:
        adapter.ingest_payload(spans)
    except OTLPIngestError as exc:
        _reject(str(exc))

    receipt, _ = IngestOTLPReceipt(
        source_label=source_label,
        profile_name=profile_name,
        audit_dir=root / ".sdd" / "audit",
        hmac_key=_load_hmac_key(),
        ingest_adapter=adapter,
    ).ingest_batch(spans)

    if as_json:
        click.echo(json.dumps(receipt.to_dict(), ensure_ascii=False))
        return

    console.print()
    console.print(f"[bold]Governance ingest[/bold] source={receipt.source_label}")
    console.print(f"  spans reported     {receipt.span_count}")
    console.print(f"  profile            {receipt.profile_name}")
    console.print(f"  arrival index      {receipt.arrival_index}")
    console.print(f"  batch digest       {receipt.batch_digest}")
    console.print(f"  chain entry        {receipt.chain_entry_hash}")
    console.print(f"  coverage           {receipt.coverage}")
    console.print(f"  [dim]{receipt.coverage_detail}[/dim]")


@govern_group.command("audit-compliance")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=".",
    show_default=True,
    help="Project root the checks read.",
)
@click.option(
    "--only",
    "only",
    multiple=True,
    metavar="ID|NAMESPACE|AREA",
    help="Run only the checks matching this id, id namespace (CMP) or area.",
)
@click.option(
    "--skip",
    "skip",
    multiple=True,
    metavar="ID",
    help="Do not run this check id. Skipping selects what runs; it suppresses no finding.",
)
@click.option(
    "--profile",
    "profile",
    default=None,
    type=click.Choice([f.value for f in ComplianceFramework], case_sensitive=False),
    help="Mark which check ids this framework requires. Selects ids only; asserts nothing.",
)
@click.option("--list", "list_only", is_flag=True, help="Print the registered check ids and exit.")
@click.option("--json-output", "as_json", is_flag=True, help="Print the report as JSON and nothing else.")
def govern_audit_cmd(
    workdir: Path,
    only: tuple[str, ...],
    skip: tuple[str, ...],
    profile: str | None,
    list_only: bool,
    as_json: bool,
) -> None:
    """Run every registered check over this install and report one finding each.

    Each finding carries a stable id, one of three verdicts -- measured,
    declared, or not measurable -- and the evidence it read. A check that only
    tests whether a key is present in a configuration file reports *declared*:
    the operator asserted the control and nothing was read that confirms it.

    There is no score and no grade. Every count names its denominator, and
    --profile selects which ids a framework requires without asserting anything
    about the result.
    """
    required: frozenset[str] = required_check_ids(ComplianceFramework(profile.lower())) if profile else frozenset()

    try:
        selected = select_check_ids(only=only, skip=skip)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    specs = {spec.check_id: spec for spec in iter_compliance_checks()}

    if list_only:
        _print_audit_catalogue([specs[cid] for cid in selected], required, as_json=as_json)
        return

    outcomes = run_compliance_checks(workdir, only=only, skip=skip)
    counts = count_by_outcome(outcomes)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "schema_version": 1,
                    "area": CMP_AREA,
                    "profile": profile.lower() if profile else None,
                    "checks_run": len(outcomes),
                    "checks": [outcome.to_dict() | {"required": outcome.check_id in required} for outcome in outcomes],
                    "counts": counts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    click.echo(f"govern audit-compliance -- area {CMP_AREA}, {len(outcomes)} checks over {workdir}")
    click.echo("")
    for outcome in outcomes:
        marker = "*" if outcome.check_id in required else " "
        state = "pass" if outcome.passed else "fail"
        verdict = outcome.verdict.value if outcome.verdict is not CheckVerdict.MEASURED else f"measured {state}"
        click.echo(f"  {marker}{outcome.check_id}  {verdict:<14}  {outcome.summary}")
    click.echo("")
    total = len(outcomes)
    for label, count in counts.items():
        click.echo(f"  {label.replace('_', ' ')}: {count} of {total} checks")
    if profile:
        click.echo("")
        click.echo(
            f"  required by profile {profile.lower()}: "
            f"{len(required & {o.check_id for o in outcomes})} of {total} ids "
            "(marked *; the profile selects ids and states nothing about the result)"
        )


def _print_audit_catalogue(
    specs: list[ComplianceCheckSpec],
    required: frozenset[str],
    *,
    as_json: bool,
) -> None:
    """Print the registered check ids without running any of them."""
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "check_id": spec.check_id,
                        "area": spec.area,
                        "asserts": spec.asserts,
                        "required": spec.check_id in required,
                    }
                    for spec in specs
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    for spec in specs:
        marker = "*" if spec.check_id in required else " "
        click.echo(f"  {marker}{spec.check_id}  {spec.area:<12}  {spec.asserts}")


def _run_verifier_key_staleness_check() -> None:
    from bernstein.core.govern.audit_sweep import CheckVerdict, check_verifier_key_staleness
    from bernstein.core.identity.http_signing import default_keystore

    try:
        outcomes = check_verifier_key_staleness(default_keystore=default_keystore())
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        click.echo(f"keystore failure: {exc}", err=True)
        raise SystemExit(1) from exc

    for outcome in outcomes:
        if outcome.verdict is CheckVerdict.NOT_MEASURABLE:
            click.echo(f"{outcome.check_id}: {outcome.summary}")
            raise SystemExit(1)
        if outcome.verdict is CheckVerdict.MEASURED and outcome.passed is False:
            click.echo(f"{outcome.check_id}: {outcome.summary}")
            raise SystemExit(2)

    click.echo("verifier keys up to date")


@govern_group.command("audit")
def governance_audit_cmd() -> None:
    """[Deprecated] Use ``bernstein govern audit-keys`` instead.

    The compliance policy library moved to ``bernstein govern audit-compliance``
    in #5075; this alias preserves the prior verifier-key staleness behaviour
    for one release and prints a deprecation notice on every invocation.
    """
    click.echo(
        "WARNING: 'bernstein govern audit' is deprecated and will be removed in v3.0.0 (#5075): "
        "use 'bernstein govern audit-keys' instead.",
        err=True,
    )
    _run_verifier_key_staleness_check()


@govern_group.command("audit-keys")
def governance_audit_keys_cmd() -> None:
    """Check whether verifier keys are stale relative to the install identity.

    Exit codes: 0 = up to date or no verifier files, 1 = keystore or verifier
    file unreadable, 2 = stale verifier key detected.
    """
    _run_verifier_key_staleness_check()


# Desired-state reconcile diff over the governed surface (#5085). Registered
# here, before the alias mirror below, so the subcommand sets stay identical.
govern_group.add_command(govern_reconcile_cmd, "reconcile")
# Inventory topology graph from the store (#5133).
govern_group.add_command(govern_inventory_cmd, "inventory")


@click.group("governance")
@click.pass_context
def governance_alias_cmd(ctx: click.Context) -> None:
    """[Deprecated] Use 'bernstein govern' instead."""
    click.echo(
        "WARNING: 'bernstein governance' is deprecated and will be removed in v4.0.0 (#5010): "
        "use 'bernstein govern' instead.",
        err=True,
    )
    ctx.forward(govern_group)


for _name, _cmd in govern_group.commands.items():
    governance_alias_cmd.add_command(_cmd, _name)


# Alias so tests can import governance_group as well
governance_group = govern_group
