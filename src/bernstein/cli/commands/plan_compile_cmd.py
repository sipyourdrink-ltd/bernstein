"""``bernstein plan compile`` - compile a spec into a gated task graph.

Attaches to the existing ``plan`` group (issue #2361). The command runs the
three-stage spec pipeline: draft (structured requirement extraction), approve
(the requirement-set hash is bound into the audit chain), and compile (a
deterministic transformation to a task graph). The compiled artefacts and, when
approved, the chain-anchored receipt are written under ``.sdd/spec/<name>/``.

The default drafter is the offline, deterministic :class:`StructuralDrafter`,
so the command makes zero model calls and produces byte-reproducible output.
Only the draft stage may ever call a model, and only once, so the pipeline
stays within the single-model-call budget.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.sdd.spec_pipeline import (
    StructuralDrafter,
    approve_requirement_set,
    compile_requirements,
    draft_requirements,
    lineage_coverage,
)

__all__ = ["plan_compile"]

# Component of a slug/name that would let the output escape the spec dir.
_UNSAFE_NAME_CHARS = frozenset({"/", "\\", "\x00"})


def _safe_name(name: str) -> str:
    """Return *name* if it is a single safe path component, else raise.

    The name becomes a directory component under ``.sdd/spec/``; a separator or
    ``..`` segment could redirect the write outside that tree, so reject it.
    """
    candidate = name.strip()
    if not candidate:
        raise click.BadParameter("name must not be empty")
    if candidate in {".", ".."} or any(ch in _UNSAFE_NAME_CHARS for ch in candidate):
        raise click.BadParameter(f"name is not a safe path component: {name!r}")
    return candidate


def _contained(base: Path, target: Path) -> Path:
    """Resolve *target* and assert it stays under *base* (realpath containment)."""
    base_real = base.resolve()
    target_real = target.resolve()
    if base_real != target_real and base_real not in target_real.parents:
        raise click.BadParameter(f"output path escapes {base_real}: {target_real}")
    return target_real


@click.command("compile")
@click.argument("spec", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--name", "name", default=None, help="Plan name / output slug. Default: spec file stem.")
@click.option("--approve", is_flag=True, default=False, help="Record an approval receipt into the audit chain.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit a JSON summary instead of a table.")
def plan_compile(spec: Path, name: str | None, approve: bool, as_json: bool) -> None:
    """Compile a requirements document into a gated task graph.

    \b
      bernstein plan compile spec.md
      bernstein plan compile spec.md --name password-reset --approve

    Stages: draft (offline structured extraction) -> approve (optional, binds
    the requirement-set hash into the audit chain) -> compile (deterministic
    task graph, each node carrying its requirement hashes).
    """
    workdir = Path.cwd()
    slug = _safe_name(name or spec.stem)

    spec_text = spec.read_text(encoding="utf-8")
    req_set = draft_requirements(spec_text, StructuralDrafter())
    if not req_set.requirements:
        console.print("[red]No EARS-shaped acceptance lines found in the spec.[/red]")
        raise SystemExit(1)

    graph = compile_requirements(req_set)
    coverage = lineage_coverage(graph)
    if not coverage.covered:
        console.print("[red]Lineage coverage gap: some task nodes carry no requirement hash.[/red]")
        raise SystemExit(1)

    spec_dir = _contained(workdir / ".sdd" / "spec", workdir / ".sdd" / "spec" / slug)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "requirements.json").write_text(
        json.dumps(req_set.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (spec_dir / "graph.json").write_text(
        json.dumps(graph.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    receipt_dict: dict[str, object] | None = None
    if approve:
        chain = AuditChainStore(workdir / ".sdd" / "audit")
        receipt, _event = approve_requirement_set(chain=chain, req_set=req_set, graph=graph)
        receipt_dict = receipt.to_dict()
        (spec_dir / "receipt.json").write_text(
            json.dumps(receipt_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    if as_json:
        console.print_json(
            data={
                "name": slug,
                "requirement_count": len(req_set.requirements),
                "requirement_set_hash": req_set.set_hash,
                "graph_hash": graph.graph_hash,
                "node_count": len(graph.nodes),
                "approved": receipt_dict is not None,
                "output_dir": str(spec_dir),
            }
        )
        return

    console.print(
        f"[green]Compiled[/green] {len(req_set.requirements)} requirement(s) -> {len(graph.nodes)} task node(s)"
    )
    console.print(f"[dim]requirement_set_hash[/dim] {req_set.set_hash}")
    console.print(f"[dim]graph_hash[/dim]           {graph.graph_hash}")
    console.print(f"[dim]output[/dim]               {spec_dir}")
    if receipt_dict is not None:
        console.print("[green]Approval receipt recorded into the audit chain.[/green]")
    else:
        console.print("[dim]Re-run with --approve to bind the requirement set into the audit chain.[/dim]")
