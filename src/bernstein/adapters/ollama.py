"""Ollama / OpenAI-compatible local LLM adapter - run coding agents without cloud API keys.

Uses Aider as the coding frontend with Ollama (or any OpenAI-compatible
local server such as vLLM, llama.cpp's HTTP server, LM Studio) as the LLM
backend.  This enables full code editing capabilities in air-gapped,
privacy-sensitive, EU-residency, or cost-zero environments.

Last verified against upstream Ollama 0.21.x on 2026-05-05.

Prerequisites:
    - Ollama: https://ollama.com  (``brew install ollama`` or
      ``curl -fsSL https://ollama.com/install.sh | sh``)
    - Aider: ``pip install aider-chat``
    - A pulled model: ``ollama pull qwen2.5-coder:7b`` (or the larger
      ``qwen3-coder``, ``deepseek-v4-flash``, ``deepseek-r1:70b``,
      ``llama3.1`` as VRAM allows).

EU-residency / vLLM note:
    For DeepSeek V4-Pro (1.6T MoE / 49B active) the single-GPU Ollama
    profile is too small; deploy via vLLM tensor-parallel and point
    ``OLLAMA_API_BASE`` at the vLLM ``/v1`` endpoint - aider/litellm's
    OpenAI-compatible path treats the two interchangeably.  Callers
    that need an EU-residency guarantee MUST pass ``eu_residency=True``
    AND pin ``base_url`` to a self-hosted (e.g. RFC-1918 / *.internal /
    EU-located) endpoint.  Combine this adapter with
    :class:`bernstein.core.security.data_residency.DataResidencyController`
    (set ``allowed_regions={EU_WEST, EU_CENTRAL}``,
    ``enforce_strict=True``) for the full Article-12 evidence story.
    See FEAT ``deepseek-v4-flash-eu`` for the full profile spec.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.adapters.plugin_sdk import AdapterCapability, AdapterPluginInfo

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)

# Default Ollama API endpoint
OLLAMA_BASE_URL = "http://localhost:11434"

# Maps Bernstein abstract model names to Ollama / OpenAI-compatible model IDs.
# Users can also pass native model IDs directly (e.g. "qwen2.5-coder:32b").
#
# DeepSeek V4 entries (added 2026-05-07 - FEAT deepseek-v4-flash-eu):
#   - ``deepseek-v4-flash`` - 284B / 13B-active MoE, MIT-licensed, fits a
#     single H100/A100; primary cheap-first arm for EU-residency runs.
#   - ``deepseek-v4-pro``   - 1.6T / 49B-active MoE, MIT-licensed, requires
#     vLLM tensor-parallel deployment (does not fit single-GPU Ollama).
#     Set ``OLLAMA_API_BASE`` to the vLLM ``/v1`` endpoint to route here.
_MODEL_MAP: dict[str, str] = {
    # Bernstein tiers → sensible local defaults
    "opus": "deepseek-r1:70b",
    "sonnet": "qwen2.5-coder:32b",
    "haiku": "qwen2.5-coder:7b",
    # Common coding-focused models
    "codellama": "codellama",
    "deepseek-coder": "deepseek-coder-v2",
    "deepseek-r1": "deepseek-r1",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "qwen2.5-coder": "qwen2.5-coder",
    "qwen3-coder": "qwen3-coder",
    "llama3.1": "llama3.1",
    "llama3.2": "llama3.2",
    "gemma3": "gemma3",
    "phi4": "phi4",
    "mistral": "mistral",
    "starcoder2": "starcoder2",
}

# Models that require a self-hosted endpoint when invoked under the
# ``eu-residency`` profile.  These are MIT-licensed open weights; running
# them against the hosted ``deepseek.com`` API leaks tokens out of the EU
# and breaks the Article-12 evidence story.  When the requested model is
# on this set OR the adapter was constructed with ``eu_residency=True``,
# :meth:`OllamaAdapter.spawn` rejects non-self-hosted endpoints with a
# structured ``RESIDENCY_VIOLATION`` error.
_EU_RESIDENCY_MODELS: frozenset[str] = frozenset(
    {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }
)


class OllamaAdapter(CLIAdapter):
    """Spawn coding agent sessions using local Ollama / OpenAI-compatible LLMs.

    Uses Aider as the coding agent with Ollama (or vLLM, llama.cpp, LM
    Studio, etc.) as the LLM provider, giving full file-editing capabilities
    without any cloud API keys.

    Model selection:
        - Pass a Bernstein tier name (opus/sonnet/haiku) → maps to a
          capable local model
        - Pass a native model ID (e.g. ``"qwen2.5-coder:7b"``,
          ``"deepseek-v4-flash"``) → used as-is
        - Override OLLAMA_BASE_URL (or pass ``base_url=``) to point at a
          remote Ollama / vLLM / OpenAI-compatible inference server

    Args:
        base_url: Ollama / OpenAI-compatible API base URL.  Defaults to
            ``http://localhost:11434``.
        eu_residency: When True, the spawn() method enforces that the
            configured endpoint is self-hosted (i.e. not the public
            ``deepseek.com`` / ``openrouter.ai`` / etc. hosted APIs).
            This is the operative deployment mode for FEAT
            ``deepseek-v4-flash-eu`` and integrates with
            :class:`bernstein.core.security.data_residency.DataResidencyController`
            for the full Article-12 evidence story.  Defaults to False so
            existing callers keep their behaviour.  Note: even when this
            flag is False, the residency guard still fires for any model
            in :data:`_EU_RESIDENCY_MODELS` because routing those to a
            hosted API would silently violate the ticket's promise.
    """

    def __init__(self, *, base_url: str = OLLAMA_BASE_URL, eu_residency: bool = False) -> None:
        super().__init__()
        self._base_url = base_url
        self._eu_residency = eu_residency

    @property
    def eu_residency(self) -> bool:
        """Return whether the EU-residency self-hosted guard is active."""
        return self._eu_residency

    def plugin_info(self) -> AdapterPluginInfo:
        """Declare the sampling surface OllamaAdapter genuinely wires.

        This adapter drives Aider (see the module docstring), which
        accepts a ``--temperature`` flag. Aider has no ``--top-p``,
        ``--top-k``, or completion ``--max-tokens`` CLI flag (its
        ``--max-chat-history-tokens`` controls a different thing - chat
        history truncation, not the completion length), so only the
        narrow temperature capability is declared -
        :func:`bernstein.adapters.plugin_sdk.ensure_sampling_params_supported`
        refuses a spawn requesting the others rather than silently
        dropping them.
        """
        return AdapterPluginInfo(
            name="ollama",
            version="0.1.0",
            author="bernstein",
            description="Ollama / OpenAI-compatible local LLM adapter (via Aider)",
            capabilities=(AdapterCapability.SUPPORTS_TEMPERATURE,),
        )

    def _resolve_model(self, model_name: str) -> str:
        """Map Bernstein model name to Ollama / OpenAI-compatible model ID."""
        return _MODEL_MAP.get(model_name, model_name)

    def _is_self_hosted_endpoint(self, base_url: str) -> bool:
        """Return True when ``base_url`` points at a self-hosted endpoint.

        Default-closed: this returns ``True`` only when the host is
        unambiguously self-hosted -- ``localhost``, an IPv4 RFC-1918
        private address (``10/8``, ``172.16/12``, ``192.168/16``), an
        IPv4/IPv6 loopback (``127/8`` or ``::1``), an IPv6 unique-local
        address (``fc00::/7``) or link-local (``fe80::/10``), or an FQDN
        ending in one of the recognised internal suffixes
        (``*.internal``, ``*.local``, ``*.svc``, ``*.cluster.local``).
        Any public IP or unrecognised hostname is treated as
        non-self-hosted because the residency profile cannot prove the
        endpoint sits inside the EU boundary.

        Implementation note: prior versions used naive string-prefix
        matching (``host.startswith("10.")``) which silently accepted
        public hostnames that *happened* to start with the prefix
        (``10.example.com``, ``192.168.evil.tld``, ``172.20.foo.com``)
        as self-hosted -- a residency-bypass for any attacker who
        controls a domain. Use :mod:`ipaddress` so the check is on the
        wire-form octet semantics, not the hostname text.

        Args:
            base_url: The configured Ollama / OpenAI-compatible base URL.

        Returns:
            True if the endpoint looks self-hosted, False otherwise.
        """
        import ipaddress
        from urllib.parse import urlparse

        host = (urlparse(base_url).hostname or "").lower()
        if not host:
            # Empty / malformed URL - fail closed under residency mode.
            return False
        if host == "localhost":
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            # Not a literal IP; fall through to FQDN-suffix allow-list.
            # ``0.0.0.0`` is intentionally NOT in the suffix list -- it is
            # the IPv4 wildcard, not loopback, and would whitelist any
            # interface the host happens to bind.
            return host.endswith((".internal", ".local", ".svc", ".cluster.local"))
        # IP-literal path: rely on stdlib semantics for v4 + v6.
        return ip.is_loopback or ip.is_private or ip.is_link_local

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
        from urllib.parse import urlparse

        from bernstein.core.security.network_policy import policy_from_env

        parsed = urlparse(self._base_url)
        policy_from_env().check(
            parsed.hostname or "localhost",
            parsed.port or 11434,
            source="adapter:ollama",
        )

        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        ollama_model = self._resolve_model(model_config.model)

        # FEAT deepseek-v4-flash-eu: when caller activates ``eu_residency``,
        # OR when the requested model is on the self-hosted-only list,
        # refuse to spawn against a non-self-hosted endpoint.  Loud,
        # structured failure: never silently fall back to a public API.
        residency_active = self._eu_residency or ollama_model in _EU_RESIDENCY_MODELS
        if residency_active and not self._is_self_hosted_endpoint(self._base_url):
            raise RuntimeError(
                "RESIDENCY_VIOLATION: model "
                f"{ollama_model!r} requires a self-hosted endpoint under the "
                f"eu-residency profile, got {self._base_url!r}. "
                "Set OLLAMA_API_BASE / OLLAMA_HOST to a self-hosted (e.g. "
                "vLLM, Ollama on a private/EU node) endpoint and retry."
            )

        # aider supports ollama via litellm: --model ollama/<model>
        # Smaller repo map keeps local model context usage manageable.
        cmd = [
            "aider",
            "--model",
            f"ollama/{ollama_model}",
            "--message",
            prompt,
            "--yes-always",
            "--auto-commits",
            "--map-tokens",
            "1024",
            "--no-auto-lint",
        ]

        if mcp_config:
            temperature = mcp_config.get("temperature")
            if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
                logger.debug("ollama adapter: wiring --temperature=%s onto aider argv", temperature)
                cmd.extend(["--temperature", str(float(temperature))])
            for dropped_key in ("top_p", "top_k", "max_tokens"):
                if mcp_config.get(dropped_key) is not None:
                    logger.warning(
                        "ollama adapter: %s=%r requested but not wired (aider has no matching "
                        "CLI flag) - ensure_sampling_params_supported should have refused this "
                        "spawn before reaching here",
                        dropped_key,
                        mcp_config.get(dropped_key),
                    )

        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=f"ollama/{ollama_model}",
        )

        # Pass OLLAMA_API_BASE so aider/litellm finds the Ollama server.
        # Strip cloud API keys so the agent doesn't accidentally use them.
        env = build_filtered_env(["OLLAMA_API_BASE", "OLLAMA_HOST"])
        env["OLLAMA_API_BASE"] = self._base_url
        env["OLLAMA_HOST"] = self._base_url

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
                    "aider not found in PATH. Install with: pip install aider-chat\n"
                    "Also ensure Ollama is running: ollama serve"
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing aider: {exc}") from exc

        # Thread the live Popen handle through so callers holding the
        # result can call ``proc.wait()`` / ``proc.poll()`` to tell a
        # live agent apart from an unreaped zombie.  Aligns this adapter
        # with codex/gemini/claude (which already threaded ``proc`` via
        # the SpawnResult); the base ``is_alive(pid)`` /proc probe alone
        # cannot distinguish those two states.
        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Ollama (local)"
