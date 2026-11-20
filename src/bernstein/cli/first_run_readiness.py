"""Read-only setup advice for a first run from the current checkout."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rich.console import Console


@dataclass(frozen=True)
class CheckoutHandoff:
    branch: str | None
    default_target: bool
    next_action: str


def checkout_handoff(root: Path) -> CheckoutHandoff:
    from bernstein.core.git_ops import current_branch, protected_default_branches, run_git

    branch = current_branch(root)
    if branch:
        protected = branch in protected_default_branches(root)
        if not protected:
            return CheckoutHandoff(branch, False, "")
        existing = run_git(["show-ref", "--verify", "--quiet", "refs/heads/bernstein/first-run"], root, timeout=5)
        if existing.ok:
            action = "Switch to an existing working branch, for example git switch bernstein/first-run."
        else:
            action = "Create a working branch with git switch -c bernstein/first-run."
        return CheckoutHandoff(branch, True, action)
    try:
        inside = run_git(["rev-parse", "--is-inside-work-tree"], root, timeout=5)
        if inside.stdout.strip() != "true":
            action = "Open an existing Git checkout, or run git init and make an initial commit."
        elif run_git(["rev-parse", "--verify", "HEAD"], root, timeout=5).ok:
            action = "HEAD is detached. Create a working branch with git switch -c bernstein/first-run."
        else:
            action = "Make an initial commit before creating a working branch."
    except (OSError, subprocess.SubprocessError):
        action = "Install Git and check that git status works in the project."
    return CheckoutHandoff(None, False, action)


def worker_handoff(worker: str, *, env: Mapping[str, str] | None = None) -> str:
    from bernstein.core.orchestration.preflight import _get_api_key_error

    if worker == "auto":
        return "Set cli in bernstein.yaml to the worker you want to check, or keep auto for runtime routing."
    if shutil.which(worker, path=env.get("PATH", "") if env is not None else None) is None:
        return f"Install the {worker} worker and make sure it is on PATH."
    problem = _get_api_key_error(worker, env=env)
    return problem[2] if problem else ""


def planner_handoff(provider: str, *, root: Path | None = None) -> str:
    if provider in ("", "none"):
        return ""
    from bernstein.core.routing.llm import API_PROVIDERS, LLMSettings, _resolve_provider_config

    if provider not in API_PROVIDERS:
        action = worker_handoff(provider)
        return f"Internal planner {provider}: {action}" if action else ""
    try:
        _resolve_provider_config(provider, LLMSettings(_env_file=(root or Path.cwd()) / ".env"))
    except ValueError:
        return f"Configure the selected {provider} planner before starting the run."
    return ""


@dataclass(frozen=True)
class ProjectHandoff:
    checkout: CheckoutHandoff
    worker: str | None
    planner: str | None
    actions: tuple[str, ...]

    def render(self, console: Console) -> None:
        console.print(
            "Project setup: " + ("action needed" if self.actions else "ready for a configured run"), markup=False
        )
        if self.checkout.branch:
            console.print(f"Working branch: {self.checkout.branch}", markup=False)
        if self.worker is not None:
            console.print(f"Worker: {self.worker}  |  Internal planner: {self.planner}", markup=False)
        for action in self.actions:
            console.print(f"  {action}", markup=False)


def project_handoff(root: Path, *, remote: bool = False, worker_env: Mapping[str, str] | None = None) -> ProjectHandoff:
    from bernstein.core.config.seed_config import SeedError
    from bernstein.core.config.seed_parser import _load_seed_mapping, _parse_seed

    checkout = checkout_handoff(root)
    actions = [checkout.next_action] if checkout.next_action else []
    config_path = root / "bernstein.yaml"
    if not config_path.is_file():
        actions.append("Run bernstein init to create bernstein.yaml.")
        return ProjectHandoff(checkout, None, None, tuple(actions))
    try:
        data = _load_seed_mapping(config_path)
        has_goal = "goal" in data
        seed = _parse_seed(config_path, apply_tuning=False, mapping=data if has_goal else {**data, "goal": "First run"})
    except (OSError, ValueError, SeedError):
        actions.append("Check bernstein.yaml with bernstein config validate before running.")
        return ProjectHandoff(checkout, None, None, tuple(actions))
    if not has_goal:
        actions.append("Edit bernstein.yaml: set a goal for bernstein run.")
    if not seed.model:
        actions.append("Set model in bernstein.yaml to a model your worker supports for the single-worker example.")
    if remote:
        actions.append("Run bernstein doctor on the machine that will run the worker.")
    else:
        actions.extend(
            action
            for action in (
                worker_handoff(seed.cli, env=worker_env),
                planner_handoff(seed.internal_llm_provider, root=root),
            )
            if action
        )
    return ProjectHandoff(checkout, seed.cli, seed.internal_llm_provider, tuple(actions))
