"""Kimchi CLI adapter (#3100)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit

#: Credentials and endpoint overrides forwarded into the spawned environment.
#: ``KIMCHI_API_KEY`` authenticates hosted execution; the two host variables
#: point local execution at an Ollama endpoint. Nothing else is forwarded.
_PROVIDER_ENV_VARS = ("KIMCHI_API_KEY", "KIMCHI_OLLAMA_HOST", "OLLAMA_HOST")

#: Keys a Kimchi config file may carry a credential under.
_CONFIG_CREDENTIAL_KEYS = ("api_key", "apiKey")


def _config_path() -> Path:
    """Return the local Kimchi config file path."""
    return Path.home() / ".config" / "kimchi" / "config.json"


def _config_credential_present() -> bool:
    """Return whether the local Kimchi config carries a usable credential.

    A file that is missing, unreadable, not JSON, not an object, or carries
    no non-empty credential key answers ``False``. Existence alone is not
    evidence: a logged-out or truncated config is exactly the case that must
    not advertise a working account.
    """
    try:
        raw = _config_path().read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    return any(str(data.get(key) or "").strip() for key in _CONFIG_CREDENTIAL_KEYS)


class KimchiAdapter(CLIAdapter):
    """Spawn and monitor Kimchi CLI sessions (#3100).

    Kimchi runs open-weight / hosted models. It is invoked as
    ``kimchi --mode acp --yolo --prompt "<task>" [--model <model>]``.

    Two properties of an orchestrated run decide how the process is spawned:

    * **Nobody is at the keyboard.** Every Bernstein worker runs unattended,
      so ``--yolo`` - the flag :class:`~bernstein.adapters._contract.DangerousModeStrategy`
      ``CLI_FLAG`` names for this adapter - is passed on every spawn. Gating
      it behind a keyword argument would leave the declaration describing a
      code path no orchestrator reaches, and the run would stall on the first
      tool-approval prompt until the timeout watchdog killed it.
    * **stdin is closed.** ``--mode acp`` puts the CLI in a mode that expects
      a JSON-RPC peer. Bernstein does not act as that peer here (see below),
      so the process is given ``DEVNULL`` rather than the orchestrator's own
      stdin: it fails immediately instead of holding a worker slot open for
      the whole timeout, and it can never consume the parent's input.

    Event-channel status. Kimchi speaks the Agent Client Protocol, which is
    what :class:`~bernstein.adapters._contract.EventChannel` ``ACP`` records
    for it. Bernstein's ACP client transport
    (:mod:`bernstein.adapters.acp_channel`) is *not* bound to a live Kimchi
    process by this adapter - as with the other ACP-declaring adapters, the
    spawn hands back a log file and the completion verdict comes from the
    commit check (:class:`~bernstein.adapters._contract.OutputMode`
    ``GIT_DIFF``). The shipped ACP coverage is ingress conformance over a
    recorded frame fixture, not a live drive.
    """

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
        self.enforce_network_policy()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "kimchi",
            "--mode",
            "acp",
            "--yolo",
            "--prompt",
            prompt,
        ]

        if model_config.model:
            cmd += ["--model", model_config.model]

        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )

        env = build_filtered_env(list(_PROVIDER_ENV_VARS))
        env["KIMCHI_TELEMETRY_ENABLED"] = "0"

        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "kimchi not found in PATH. Install with `brew install getkimchi/tap/kimchi`"
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing kimchi: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Kimchi"

    def detect_tier(self) -> ApiTierInfo | None:
        """Report a usable Kimchi account, or ``None`` when none is proven.

        A tier is reported only when a credential is actually present: a
        non-empty ``KIMCHI_API_KEY``, or a config file that parses and
        carries a non-empty key. The CLI does not expose subscription
        details, so any proven credential reports ``PRO`` (the common paid
        tier), mirroring the other credential-only adapters.

        Returns:
            :class:`ApiTierInfo` when a credential is proven, otherwise
            ``None``.
        """
        if not os.environ.get("KIMCHI_API_KEY", "").strip() and not _config_credential_present():
            return None

        return ApiTierInfo(
            provider=ProviderType.KIMCHI,
            tier=ApiTier.PRO,
            rate_limit=RateLimit(
                requests_per_minute=60,
                tokens_per_minute=30_000,
            ),
            is_active=True,
        )
