"""``bernstein admission``: check the executor admission policy (#4907).

``bernstein.yaml`` may declare an ``admission:`` block naming the
adapters, models, endpoints and sandbox tiers this repository is allowed
to spawn on. The gate that enforces it runs inside the spawner, which
means an operator only learns that a role is refused when a run reaches
that role.

``bernstein admission check`` answers the same question ahead of time:
it derives one executor identity per configured role from the config
alone, evaluates each against the declared policy, and prints the
decision and the deciding rule id. Nothing is spawned. The exit status
is non-zero when any row is refused, so the command doubles as a CI
check that a config change did not make a role unspawnable::

    bernstein admission check
    bernstein admission check --adapter codex --model gpt-5-codex
    bernstein admission check --json
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from bernstein.core.config.seed_config import SeedError
from bernstein.core.config.seed_parser import parse_seed
from bernstein.core.security.executor_admission import (
    AdmissionPolicy,
    AdmissionPolicyError,
    AdmissionSubject,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bernstein.core.config.seed_config import SeedConfig

_SEED_NAME = "bernstein.yaml"
_DEFAULT_ROLE = "default"
_COLUMNS = ("ROLE", "ADAPTER", "MODEL", "ENDPOINT", "SANDBOX", "DECISION", "RULE")


@click.group("admission")
def admission_group() -> None:
    """Executor admission policy: which executors this repository may use.

    \b
      bernstein admission check
      bernstein admission check --adapter codex --model gpt-5-codex
    """


@admission_group.command("check")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Repository root holding bernstein.yaml.",
)
@click.option("--role", default=None, help="Evaluate a single role instead of every configured role.")
@click.option("--adapter", default=None, help="Override the adapter axis for the evaluated subject.")
@click.option("--model", default=None, help="Override the model axis for the evaluated subject.")
@click.option("--endpoint", default=None, help="Override the endpoint base-URL axis.")
@click.option("--sandbox", default=None, help="Override the sandbox-tier axis.")
@click.option("--task-type", "task_type", default=None, help="Override the task-type axis.")
@click.option("--json", "as_json", is_flag=True, help="Emit the decision table as JSON.")
def admission_check(
    workdir: str,
    role: str | None,
    adapter: str | None,
    model: str | None,
    endpoint: str | None,
    sandbox: str | None,
    task_type: str | None,
    as_json: bool,
) -> None:
    """Print the admission decision for each configured role.

    Exits ``1`` when any evaluated subject is refused, or when the
    config or the policy cannot be read.
    """
    root = Path(workdir).resolve()
    try:
        seed = parse_seed(root / _SEED_NAME)
    except SeedError as exc:
        raise click.ClickException(f"could not read {root / _SEED_NAME}: {exc}") from exc

    policy = seed.admission
    if policy is None:
        try:
            policy = AdmissionPolicy.load(root)
        except AdmissionPolicyError as exc:  # pragma: no cover - parse_seed catches first
            raise click.ClickException(str(exc)) from exc
    if policy is None:
        click.echo("No admission policy declared - every executor the config selects is allowed.")
        return

    overrides = {
        "adapter": adapter,
        "model": model,
        "endpoint": endpoint,
        "sandbox": sandbox,
        "task_type": task_type,
    }
    rows: list[dict[str, Any]] = []
    refused = 0
    for subject in _subjects(seed, role=role, overrides=overrides):
        decision = policy.evaluate(subject)
        if not decision.allowed:
            refused += 1
        row = decision.subject.as_dict()
        row["decision"] = "allow" if decision.allowed else "refuse"
        row["rule_id"] = decision.rule_id
        row["reason"] = decision.reason
        rows.append(row)

    if as_json:
        click.echo(_json.dumps({"mode": policy.mode.value, "rows": rows}, indent=2, sort_keys=True))
    else:
        _print_table(policy.mode.value, rows)

    if refused:
        raise SystemExit(1)


def _subjects(
    seed: SeedConfig,
    *,
    role: str | None,
    overrides: dict[str, str | None],
) -> list[AdmissionSubject]:
    """Derive one subject per role from the config, applying overrides.

    The derivation is config-only and deterministic: it reads the same
    ``role_model_policy`` entries and ``sandbox:`` block the spawner
    resolves from, so a row here names the executor a run would select.
    Axes the config leaves unpinned (``cli: auto``, no model) are
    reported verbatim; pass the matching option to check a concrete case.
    """
    policy_by_role: dict[str, dict[str, Any]] = dict(seed.role_model_policy or {})
    roles = [role] if role else sorted(policy_by_role) or [_DEFAULT_ROLE]
    sandbox_tier = _sandbox_tier(seed)
    subjects: list[AdmissionSubject] = []
    for name in roles:
        entry = policy_by_role.get(name, {})
        subjects.append(
            AdmissionSubject(
                role=name,
                adapter=overrides["adapter"] or str(entry.get("cli") or entry.get("provider") or seed.cli),
                model=overrides["model"] or str(entry.get("model") or seed.model or ""),
                endpoint=overrides["endpoint"] or str(entry.get("base_url") or ""),
                sandbox=overrides["sandbox"] or sandbox_tier,
                task_type=overrides["task_type"] or "standard",
            )
        )
    return subjects


def _sandbox_tier(seed: SeedConfig) -> str:
    """Return the sandbox tier a run of this config would spawn under.

    Mirrors ``AgentSpawner._admission_sandbox_tier``: an enabled
    ``sandbox:`` block names its container runtime, otherwise runs use
    worktree isolation. A ``--sandbox`` flag on ``bernstein run``
    overrides this at runtime; pass ``--sandbox`` here to check that.
    """
    sandbox = seed.sandbox
    if sandbox is not None and getattr(sandbox, "enabled", False):
        return str(sandbox.runtime)
    return "worktree"


def _print_table(mode: str, rows: list[dict[str, Any]]) -> None:
    """Render the decision rows as an aligned plain-text table."""
    click.echo(f"Admission policy mode: {mode}")
    keys = ("role", "adapter", "model", "endpoint", "sandbox", "decision", "rule_id")
    cells = [[str(row[key]) or "-" for key in keys] for row in rows]
    widths = [max(len(_COLUMNS[i]), *(len(cell[i]) for cell in cells)) for i in range(len(keys))]
    click.echo("  ".join(_COLUMNS[i].ljust(widths[i]) for i in range(len(keys))).rstrip())
    for cell in cells:
        click.echo("  ".join(cell[i].ljust(widths[i]) for i in range(len(keys))).rstrip())
