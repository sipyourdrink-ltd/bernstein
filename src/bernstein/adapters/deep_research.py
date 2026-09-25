"""Shared machinery for the deep-research adapters.

Each deep-research agent (gpt-researcher, Tongyi DeepResearch, ...) has its
own adapter module; this one holds what they share:

* the gateway contract - every agent reaches its models through one
  OpenAI-compatible endpoint with its own key, read from
  ``BERNSTEIN_<AGENT>_OPENAI_BASE_URL``, ``BERNSTEIN_<AGENT>_OPENAI_API_KEY``
  and ``BERNSTEIN_<AGENT>_MODEL``. One agent's key never serves another, and
  error messages name the missing variables, never their values;
* the spawn path - the agent runs a standalone runner script with its own
  interpreter, so the agent's dependencies never enter bernstein's
  environment;
* the typed read-back of the run artifact (:mod:`.deep_research_artifact`),
  with its digests recomputed, so a report edited after the run is
  :attr:`ResearchTerminalState.TAMPERED`, not a success.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bernstein.adapters import deep_research_artifact as artifact
from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.models import ModelConfig

_GATEWAY_SUFFIXES = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "MODEL")


class GatewayConfigError(RuntimeError):
    """A required ``BERNSTEIN_<AGENT>_*`` variable is unset. Names only."""


class ResearchTerminalState(StrEnum):
    """Terminal outcome of a deep-research run. Never free text."""

    OK = "ok"
    INCONCLUSIVE = "inconclusive"
    DRIVER_FAILURE = "driver_failure"
    TAMPERED = "tampered"


class DeepResearchDriverError(RuntimeError):
    """The agent's runner could not be started."""

    def __init__(self, message: str) -> None:
        self.terminal_state = ResearchTerminalState.DRIVER_FAILURE
        super().__init__(message)


def env_prefix(slug: str) -> str:
    """``gpt-researcher`` -> ``BERNSTEIN_GPT_RESEARCHER_``."""
    return "BERNSTEIN_" + slug.upper().replace("-", "_") + "_"


def gateway_env_names(slug: str) -> tuple[str, str, str]:
    """The base-URL, key and model variable names for one agent."""
    prefix = env_prefix(slug)
    base, key, model = (prefix + s for s in _GATEWAY_SUFFIXES)
    return base, key, model


@dataclass(frozen=True)
class GatewayConfig:
    """One agent's endpoint, key and model. ``repr`` hides the key."""

    base_url: str
    api_key: str = field(repr=False)
    model: str


def read_gateway(slug: str, environ: Mapping[str, str]) -> GatewayConfig:
    """Read one agent's gateway settings, or name every missing variable."""
    names = gateway_env_names(slug)
    values = [(environ.get(n) or "").strip() for n in names]
    missing = [n for n, v in zip(names, values, strict=True) if not v]
    if missing:
        raise GatewayConfigError(f"{slug}: set {', '.join(missing)} (gateway endpoint, per-agent key, model)")
    return GatewayConfig(base_url=values[0], api_key=values[1], model=values[2])


def require_env(name: str, environ: Mapping[str, str], why: str) -> str:
    value = (environ.get(name) or "").strip()
    if not value:
        raise GatewayConfigError(f"set {name} ({why})")
    return value


@dataclass(frozen=True)
class ResearchRun:
    """A run artifact as read back and verified."""

    state: ResearchTerminalState
    sources_count: int = 0
    wall_seconds: float = 0.0
    report_path: Path | None = None
    errors: tuple[str, ...] = ()
    record: dict[str, Any] = field(default_factory=dict)


def load_research_run(run_dir: Path) -> ResearchRun:
    """Read ``run.json`` and recompute the digests of the files it binds."""
    record, errors = artifact.verify_run(run_dir)
    if record is None:
        return ResearchRun(state=ResearchTerminalState.DRIVER_FAILURE, errors=tuple(errors))
    state = ResearchTerminalState.TAMPERED if errors else ResearchTerminalState(record["state"])
    return ResearchRun(
        state=state,
        sources_count=int(record.get("sources_count", 0)),
        wall_seconds=float(record.get("wall_seconds", 0.0)),
        report_path=run_dir / artifact.REPORT_FILE,
        errors=tuple(errors),
        record=record,
    )


class DeepResearchAdapter(CLIAdapter):
    """Spawn path shared by the per-agent deep-research adapters.

    A subclass names its agent (``slug``), its runner script, the variables it
    lets through, and how the gateway settings map onto the agent's own
    configuration (:meth:`build_env`) and command line (:meth:`build_command`).
    """

    slug: ClassVar[str]
    runner_script: ClassVar[str]
    display_name: ClassVar[str]
    #: Agent-specific settings let through the env filter (search-provider
    #: keys, depth knobs). Values come from the operator's environment.
    passthrough_env: ClassVar[tuple[str, ...]] = ()

    is_multimodal = False

    def runner_path(self) -> Path:
        return Path(__file__).with_name(self.runner_script)

    def python(self, environ: Mapping[str, str]) -> str:
        """The agent's interpreter: ``BERNSTEIN_<AGENT>_PYTHON``, else ``python3``."""
        return (environ.get(env_prefix(self.slug) + "PYTHON") or "").strip() or "python3"

    def build_env(self, environ: Mapping[str, str]) -> dict[str, str]:
        raise NotImplementedError

    def build_command(self, prompt_file: Path, run_dir: Path, environ: Mapping[str, str]) -> list[str]:
        raise NotImplementedError

    def run_dir(self, workdir: Path, session_id: str) -> Path:
        return workdir / ".sdd" / self.slug / session_id

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        self.refuse_multimodal_if_needed(multimodal_context)
        environ = dict(os.environ)
        run_dir = self.run_dir(workdir, session_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = run_dir / "prompt.txt"
        prompt_file.write_text(prompt + (f"\n\n{system_addendum}" if system_addendum else ""), encoding="utf-8")
        cmd = self.build_command(prompt_file, run_dir, environ)
        env = self.build_env(environ)

        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        wrapped = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=workdir / ".sdd" / "runtime" / "pids",
            workdir=workdir,
            log_path=log_path,
            model=read_gateway(self.slug, environ).model,
        )
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=self._get_preexec_fn(),
                )
            except (FileNotFoundError, PermissionError) as exc:
                raise DeepResearchDriverError(f"{self.slug}: cannot start {cmd[0]!r}: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def _filtered(self, environ: Mapping[str, str]) -> dict[str, str]:
        env = build_filtered_env(self.passthrough_env)
        env.update({k: environ[k] for k in self.passthrough_env if environ.get(k)})
        return env

    def name(self) -> str:
        return self.display_name
