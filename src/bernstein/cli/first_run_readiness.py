"""Read-only setup advice for a first run from the current checkout."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rich.console import Console


@dataclass(frozen=True)
class CheckoutHandoff:
    branch: str | None
    default_target: bool
    next_action: str


def merge_target(root: Path) -> tuple[str | None, bool]:
    from bernstein.core.git_ops import current_branch, protected_default_branches

    branch = current_branch(root)
    return branch, branch is not None and branch in protected_default_branches(root)


def _working_branch_action(root: Path) -> str:
    from bernstein.core.git_ops import run_git

    refs = run_git(["for-each-ref", "--format=%(refname:lstrip=2)", "refs/heads"], root, timeout=5)
    if not refs.ok:
        return "Check git branch and create a new working branch from the current commit."
    names = set(refs.stdout.splitlines())
    base = "bernstein-first-run" if "bernstein" in names else "bernstein/first-run"
    candidate = base
    suffix = 1
    while candidate in names or any(name.startswith(candidate + "/") for name in names):
        suffix += 1
        candidate = f"{base}-{suffix}"
    return f"Create a working branch with git switch -c {candidate}."


def checkout_handoff(root: Path) -> CheckoutHandoff:
    from bernstein.core.git_ops import run_git

    branch, protected = merge_target(root)
    try:
        if branch:
            return CheckoutHandoff(branch, protected, _working_branch_action(root) if protected else "")
        inside = run_git(["rev-parse", "--is-inside-work-tree"], root, timeout=5)
        if inside.stdout.strip() != "true":
            action = "Open an existing Git checkout, or run git init and make an initial commit."
        elif run_git(["rev-parse", "--verify", "HEAD"], root, timeout=5).ok:
            action = f"HEAD is detached. {_working_branch_action(root)}"
        else:
            action = "Make an initial commit before creating a working branch."
    except (OSError, subprocess.SubprocessError):
        action = "Check that git status works and select a working branch before starting."
    return CheckoutHandoff(branch, protected, action)


def _cli_login(worker: str, binary: str, env: Mapping[str, str]) -> bool:
    args = ["auth", "status", "--json"] if worker == "claude" else ["login", "status"]
    try:
        result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=5, env=dict(env))
        if result.returncode:
            return False
        if worker == "claude":
            status = json.loads(result.stdout)
            return isinstance(status, dict) and status.get("loggedIn") is True
        lines = (result.stdout + "\n" + result.stderr).splitlines()
        return any(line.strip().lower().startswith("logged in") for line in lines)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _codex_custom_provider(env: Mapping[str, str]) -> bool | None:
    """Check a selected custom provider offline. None defers to first-party login."""
    home = env.get("HOME") or env.get("USERPROFILE")
    codex_home = env.get("CODEX_HOME") or (Path(home) / ".codex" if home else None)
    if not codex_home:
        return None
    try:
        with (Path(codex_home) / "config.toml").open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return False
    name = config.get("model_provider")
    providers = config.get("model_providers")
    if not isinstance(name, str) or name == "openai" or not isinstance(providers, dict):
        return None
    provider = providers.get(name)
    if not isinstance(provider, dict):
        return False
    endpoint = provider.get("base_url")
    if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
        return False
    if provider.get("wire_api", "responses") != "responses" or "auth" in provider or "gateway_oauth" in provider:
        return False
    if provider.get("requires_openai_auth"):
        return None
    key = provider.get("env_key")
    return key is None or (isinstance(key, str) and bool(env.get(key)))


def worker_handoff(worker: str, *, env: Mapping[str, str] | None = None) -> str:
    from bernstein.adapters._contract import AuthBasis, ContractSpec
    from bernstein.core.orchestration.preflight import _check_qwen_auth

    if worker == "auto":
        return "Set cli in bernstein.yaml to the worker you want to check, or keep auto for runtime routing."
    source = os.environ if env is None else env
    try:
        contract = ContractSpec.load(worker)
    except FileNotFoundError:
        contract = None
    executable = contract.binary if contract is not None else worker
    binary = shutil.which(executable, path=source.get("PATH", ""))
    if binary is None:
        return f"Install {executable} for the {worker} worker and make sure it is on PATH."
    if worker == "codex":
        custom = _codex_custom_provider(source)
        if custom is not None:
            return "" if custom else "Could not confirm the Codex custom provider. Check its config.toml settings."
    if worker in {"claude", "codex"}:
        key = "ANTHROPIC_API_KEY" if worker == "claude" else "OPENAI_API_KEY"
        if source.get(key) or _cli_login(worker, binary, source):
            return ""
        login = "claude auth login" if worker == "claude" else "codex login"
        return f"Could not confirm the {worker} login. Run {login} or configure {key}."
    if worker == "gemini":
        if source.get("GEMINI_API_KEY") or source.get("GOOGLE_API_KEY"):
            return ""
        return "Could not confirm the Gemini login. Check your Gemini sign-in or configure GEMINI_API_KEY."
    if worker == "qwen":
        problem = _check_qwen_auth(env=source)
        return problem[2] if problem else ""
    if contract is not None and (
        contract.auth_basis == AuthBasis.LOCAL
        or (contract.auth_basis == AuthBasis.API_KEY and any(source.get(key) for key in contract.auth_secret_envs))
    ):
        return ""
    return f"Could not confirm the {worker} setup. Check the worker configuration before starting."


def operator_installation_checks(
    checks: list[dict[str, Any]], *, env: Mapping[str, str] | None
) -> list[dict[str, Any]]:
    """Present a matched installation and login without mutating the recorded observations."""
    result = [row.copy() for row in checks]
    rows = {row["name"]: row for row in result}
    installed = {
        name.removeprefix("Adapter: ") for name, row in rows.items() if name.startswith("Adapter: ") and row["ok"]
    }
    for worker in installed:
        row = rows.get(f"Auth: {worker}")
        if row is not None:
            problem = worker_handoff(worker, env=env)
            row.update(
                ok=not problem,
                detail="Worker access configured" if not problem else "Worker access unconfirmed",
                fix=problem,
            )
    matching = any(rows.get(f"Auth: {worker}", {}).get("ok") for worker in installed)
    if "Ready to run" in rows:
        ok = bool(rows.get("Python version", {}).get("ok") and matching)
        rows["Ready to run"].update(
            ok=ok,
            detail="Python and a matching worker are configured"
            if ok
            else "Python or a matching worker login is missing",
            fix="" if ok else "Check the Python version and configure the login for an installed worker.",
        )
    return result


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
    from bernstein.cli.helpers import find_seed_file
    from bernstein.core.config.seed_config import SeedError
    from bernstein.core.config.seed_parser import _load_seed_mapping, _parse_seed

    checkout = checkout_handoff(root)
    actions = [checkout.next_action] if checkout.next_action else []
    config_path = find_seed_file(root)
    if config_path is None:
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
