"""Read-only setup advice for a first run from the current checkout."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tomllib
from contextlib import suppress
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


def _gemini_handoff(env: Mapping[str, str]) -> str:
    """Check the sign-in modes accepted by run preflight without opening credential files."""
    if env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"):
        return ""
    home = env.get("HOME") or env.get("USERPROFILE")
    selected_adc = env.get("GOOGLE_APPLICATION_CREDENTIALS")
    adc = None
    if selected_adc:
        adc = Path(selected_adc)
    elif os.name == "nt" and (appdata := env.get("APPDATA")):
        adc = Path(appdata) / "gcloud" / "application_default_credentials.json"
    elif os.name != "nt" and home:
        adc = Path(home) / ".config" / "gcloud" / "application_default_credentials.json"
    action = "Could not confirm Gemini access. Check your Gemini sign-in, application default credentials, or API key."
    if adc is not None:
        with suppress(OSError):
            info = adc.stat()
            if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                if selected_adc:
                    return ""
                action = f"Set GOOGLE_APPLICATION_CREDENTIALS to {adc} in the shell used for the interactive run."
    if home:
        accounts = Path(home) / ".gemini" / "google_accounts.json"
        with suppress(OSError, ValueError):
            metadata = json.loads(accounts.read_text(encoding="utf-8"))
            active = metadata.get("active") if isinstance(metadata, dict) else None
            if isinstance(active, str) and active.strip():
                return ""
    return action


def _codex_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError:
        return {}


def _selected_codex_provider(config: Mapping[str, Any]) -> tuple[object, object]:
    name = config.get("model_provider", "openai")
    providers = config.get("model_providers")
    provider = providers.get(name) if isinstance(name, str) and isinstance(providers, dict) else None
    return name, provider


def _codex_handoff(binary: str, source: Mapping[str, str]) -> str:
    """Check the selected provider and the evidence Bernstein uses to start the worker."""
    from bernstein.core.agents.spawner_env import build_spawner_env

    env = build_spawner_env("codex", base_env=source)
    home = env.get("HOME") or env.get("USERPROFILE")
    standard = Path(home) / ".codex" / "config.toml" if home else None
    invalid = "Could not confirm the Codex custom provider. Check its config.toml settings."
    try:
        config = _codex_config(standard) if standard else {}
        name, provider = _selected_codex_provider(config)
        override = source.get("CODEX_HOME")
        alternate = Path(override) / "config.toml" if override else standard
        if (
            alternate
            and alternate != standard
            and _selected_codex_provider(_codex_config(alternate)) != (name, provider)
        ):
            destination = standard or "~/.codex/config.toml"
            return f"Configure the selected Codex provider and a top-level model in {destination} for Bernstein."
    except (OSError, ValueError):
        return invalid
    action = "Could not confirm the Codex login. Run codex login or configure OPENAI_API_KEY."
    if name != "openai":
        if not isinstance(provider, dict):
            return invalid
        endpoint = provider.get("base_url")
        if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
            return invalid
        if provider.get("wire_api", "responses") != "responses" or "auth" in provider or "gateway_oauth" in provider:
            return invalid
        key = provider.get("env_key")
        if key is not None and (not isinstance(key, str) or not env.get(key)):
            return "Configure the custom provider's env_key with a key available to the Codex worker."
        if not provider.get("requires_openai_auth"):
            model = config.get("model")
            if isinstance(model, str) and model.strip():
                return ""
            action = f"Set a top-level model in {standard} so Bernstein can start the configured Codex provider."
    if env.get("OPENAI_API_KEY") or _cli_login("codex", binary, env):
        return ""
    return action


def worker_handoff(worker: str, *, env: Mapping[str, str] | None = None) -> str:
    from bernstein.adapters._contract import AuthBasis, ContractSpec
    from bernstein.core.orchestration.preflight import _check_qwen_auth

    if worker == "mock":
        return ""
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
    if executable != worker and shutil.which(worker, path=source.get("PATH", "")) is None:
        return f"Run preflight also needs the {worker} command on PATH before it can start {executable}."
    if worker == "codex":
        return _codex_handoff(binary, source)
    if worker == "claude":
        if source.get("ANTHROPIC_API_KEY") or _cli_login(worker, binary, source):
            return ""
        return "Could not confirm the Claude login. Run claude auth login or configure ANTHROPIC_API_KEY."
    if worker == "gemini":
        from bernstein.adapters.gemini import BINARY_ENV_VAR

        override = source.get(BINARY_ENV_VAR, "").strip()
        if override and shutil.which(override, path=source.get("PATH", "")) is None:
            return f"Set {BINARY_ENV_VAR} to an installed Gemini CLI or unset it to use gemini."
        return _gemini_handoff(source)
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
