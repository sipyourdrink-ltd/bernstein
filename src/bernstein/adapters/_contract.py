"""Adapter contract loader and capability checker.

For every Bernstein adapter we ship a YAML contract under
``tests/contract/contracts/<adapter>.yaml`` describing the *required*
surface of the upstream CLI binary - the flags and subcommands the
adapter always passes when it invokes the CLI.

This module loads those contracts and asserts the local binary's
``--help`` output still advertises every required token. When a secret
named by ``auth.secret_env`` is set and the contract lists required
models, we additionally run the CLI's configured model-list command
and check each entry of ``expected_models.required_present`` appears.

Design notes (refined per issue #1291):

* **Capability assertions only.** We do not snapshot ``--help`` output.
  Upstream CLIs reshuffle their help text frequently; a literal-byte
  diff produces noise that overwhelms the rare real regression.
* **Drift is a hard fail.** Missing required flag -> exit 2. There is
  no daily-batched "auto-fix" PR.
* **No new repo secrets required.** Adapters whose model-presence check
  needs a secret degrade to help-only coverage when the secret is
  absent; the workflow records that fact for operator visibility.

Refs: #1291.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypedDict

import yaml

# Repo-root anchor. We compute the repo root from this file's location so
# the loader works under editable installs and from the wheel-installed
# package (in which case the contracts simply aren't packaged and the
# loader raises FileNotFoundError, the expected behaviour off-dev).
_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parents[3]
CONTRACTS_DIR = _REPO_ROOT / "tests" / "contract" / "contracts"

# Per-subprocess timeouts. Plenty for any well-behaved CLI.
_HELP_TIMEOUT_SECONDS = 30
_MODELS_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class ContractSpec:
    """Parsed contract YAML for a single adapter."""

    adapter: str
    binary: str
    install_method: str
    install_spec: str
    auth_required_for_help: bool
    auth_required_for_models: bool
    auth_secret_env: str
    required_flags: tuple[str, ...]
    required_subcommands: tuple[str, ...]
    help_command: tuple[str, ...]
    models_command: tuple[str, ...]
    models_required_present: tuple[str, ...]
    #: CLI flag that accepts a caller-supplied session id (for example
    #: ``"--session-id"``), or ``None`` when the upstream CLI does not let
    #: the caller pin one. Adapters with a flag receive the deterministic
    #: id derived by :func:`bernstein.adapters.session_id.derive_session_id`
    #: at spawn time; adapters without one have the derived id recorded in
    #: orchestrator state for cross-reference but pass no flag. See
    #: ``docs/adapters/session_isolation.md``.
    session_id_flag: str | None = None
    #: Full ordered allow-list of secret env vars the adapter accepts (highest
    #: precedence first). ``auth_secret_env`` is the highest-precedence entry,
    #: kept singular for back-compat; this carries the complete set when the
    #: contract lists more than one.
    auth_secret_envs: tuple[str, ...] = ()

    @classmethod
    def load(cls, name: str, contracts_dir: Path | None = None) -> ContractSpec:
        """Load a contract by adapter name."""
        base = contracts_dir if contracts_dir is not None else CONTRACTS_DIR
        path = base / f"{name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"No contract found for adapter {name!r} at {path}")
        with path.open("r", encoding="utf-8") as fh:
            data: dict[str, Any] = yaml.safe_load(fh) or {}

        install = data.get("install") or {}
        auth = data.get("auth") or {}
        expected = data.get("expected_models") or {}
        raw_session_flag = data.get("session_id_flag")
        session_id_flag = str(raw_session_flag) if raw_session_flag else None
        # ``secret_env`` accepts either a single name or an ordered allow-list
        # (highest precedence first). The singular ``auth_secret_env`` mirrors
        # the highest-precedence entry for back-compat with existing consumers.
        raw_secret = auth.get("secret_env")
        if isinstance(raw_secret, (list, tuple)):
            secret_envs = tuple(str(s) for s in raw_secret if s)
        elif raw_secret:
            secret_envs = (str(raw_secret),)
        else:
            secret_envs = ()
        return cls(
            adapter=str(data.get("adapter", name)),
            binary=str(data.get("binary", name)),
            install_method=str(install.get("method", "")),
            install_spec=str(install.get("spec", "")),
            auth_required_for_help=bool(auth.get("required_for_help", False)),
            auth_required_for_models=bool(auth.get("required_for_models", False)),
            auth_secret_env=secret_envs[0] if secret_envs else "",
            required_flags=tuple(data.get("required_flags") or ()),
            required_subcommands=tuple(data.get("required_subcommands") or ()),
            help_command=tuple(data.get("help_command") or ()),
            models_command=tuple(expected.get("command") or ()),
            models_required_present=tuple(expected.get("required_present") or ()),
            session_id_flag=session_id_flag,
            auth_secret_envs=secret_envs,
        )

    def resolved_help_command(self) -> list[str]:
        """The argv to run for the capability check.

        Defaults to ``[binary, "--help"]``. Contracts whose flags live
        under a subcommand can override this with an explicit
        ``help_command`` list (typically ``[binary, "<sub>", "--help"]``).
        """
        if self.help_command:
            return list(self.help_command)
        return [self.binary, "--help"]


@dataclass
class ContractResult:
    """Outcome of running ``check_contract``."""

    adapter: str
    binary: str
    binary_installed: bool
    help_exit_code: int = 0
    capability_failures: list[str] = field(default_factory=list)
    model_failures: list[str] = field(default_factory=list)
    models_checked: bool = False
    skipped_reason: str = ""
    runtime_failure: str = ""

    @property
    def passed(self) -> bool:
        """True when binary is present and no capability/model/runtime failures."""
        if not self.binary_installed:
            return False
        if self.runtime_failure:
            return False
        return not self.capability_failures and not self.model_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "binary": self.binary,
            "binary_installed": self.binary_installed,
            "help_exit_code": self.help_exit_code,
            "capability_failures": self.capability_failures.copy(),
            "model_failures": self.model_failures.copy(),
            "models_checked": self.models_checked,
            "skipped_reason": self.skipped_reason,
            "runtime_failure": self.runtime_failure,
            "passed": self.passed,
        }


# Subprocess helpers --------------------------------------------------------


def _sandbox_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal env for help/model subprocesses.

    Equivalent to ``env -i`` plus the runtime variables a CLI typically
    needs (``PATH``, ``HOME``, locale, ``TERM``). Auth-bearing variables
    are passed through only when ``extra`` opts them in - the help check
    deliberately runs without auth.
    """
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "LOGNAME")
    env: dict[str, str] = {}
    for key in keep:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    # Discourage CLIs from phoning home or updating themselves.
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("DO_NOT_TRACK", "1")
    env.setdefault("TERM", "dumb")
    if extra:
        env.update(extra)
    return env


def _run_capture(
    cmd: list[str],
    *,
    timeout: int,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run ``cmd``, capture combined stdout+stderr. Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env if env is not None else _sandbox_env(),
            check=False,
        )
    except FileNotFoundError:
        return 127, f"<binary {cmd[0]!r} not found in PATH>\n"
    except subprocess.TimeoutExpired as exc:
        partial_out = exc.stdout or ""
        partial_err = exc.stderr or ""
        if isinstance(partial_out, bytes):  # pragma: no cover -- defensive
            partial_out = partial_out.decode("utf-8", errors="replace")
        if isinstance(partial_err, bytes):  # pragma: no cover -- defensive
            partial_err = partial_err.decode("utf-8", errors="replace")
        return 124, partial_out + partial_err + f"\n<timeout after {timeout}s>\n"
    except OSError as exc:
        return 1, f"<exec error: {exc}>\n"
    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, combined


# Capability evaluation -----------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _capability_failures(spec: ContractSpec, help_text: str) -> list[str]:
    """Compute the list of human-readable capability failures.

    Flag match is case-insensitive substring. The leading dashes already
    make a flag unambiguous. Subcommand match is case-insensitive and
    requires a token boundary (start/end of line or whitespace) so that
    ``runs`` does not falsely satisfy ``run``.
    """
    failures: list[str] = []
    haystack = _strip_ansi(help_text)
    haystack_lower = haystack.lower()
    for flag in spec.required_flags:
        if flag.lower() not in haystack_lower:
            failures.append(f"missing required flag {flag!r} in `{spec.binary} --help`")
    for sub in spec.required_subcommands:
        pattern = rf"(?im)(^|\s){re.escape(sub)}(\s|$)"
        if not re.search(pattern, haystack):
            failures.append(f"missing required subcommand {sub!r} in `{spec.binary} --help`")
    return failures


def _model_failures(spec: ContractSpec, models_text: str) -> list[str]:
    """List required models missing from the CLI's model-list output."""
    failures: list[str] = []
    haystack = _strip_ansi(models_text).lower()
    for model in spec.models_required_present:
        if model.lower() not in haystack:
            failures.append(f"model {model!r} not present in `{' '.join(spec.models_command)}` output")
    return failures


def _secret_present(env_name: str) -> bool:
    """True iff a non-empty env var with that name is set."""
    if not env_name:
        return False
    value = os.environ.get(env_name)
    return bool(value and value.strip())


# Top-level checker ---------------------------------------------------------


def check_contract(spec: ContractSpec) -> ContractResult:
    """Evaluate the contract against the local environment.

    Returns a populated ``ContractResult``. The function never raises:
    every failure mode lands in ``capability_failures`` /
    ``model_failures`` / ``skipped_reason``.
    """
    result = ContractResult(adapter=spec.adapter, binary=spec.binary, binary_installed=False)

    if not spec.binary:
        result.skipped_reason = "contract has no binary"
        return result

    binary_path = shutil.which(spec.binary)
    if binary_path is None:
        result.skipped_reason = f"{spec.binary} not installed"
        return result
    result.binary_installed = True

    # 1. ``<cli> --help`` must succeed and advertise every required token.
    if spec.auth_required_for_help and not _secret_present(spec.auth_secret_env):
        result.skipped_reason = f"--help requires {spec.auth_secret_env or '<auth>'} which is unset; skipping"
        return result

    rc, help_text = _run_capture(spec.resolved_help_command(), timeout=_HELP_TIMEOUT_SECONDS)
    result.help_exit_code = rc
    if rc == 127:
        # Race between shutil.which() and spawn - extremely rare but
        # we report it cleanly.
        result.binary_installed = False
        result.skipped_reason = help_text.strip()
        return result

    # Guard: a non-zero help exit that fails to advertise the required
    # contract surface is a CLI runtime failure, not contract drift.
    # Reporting every required flag as "missing" against an empty (or
    # truncated) haystack produces misleading drift issues (one failure
    # line per required flag) when the real problem is a broken --help.
    # Cover two patterns seen in real CI:
    #   * help_text empty (CLI crashed before emitting anything).
    #   * help_text non-empty but ALL required flags missing (CLI emitted
    #     a stub or error preamble and bailed before the flag section).
    # Surface the runtime failure on a dedicated field so the CLI can
    # exit with a "checker error" status rather than a drift status; the
    # workflow distinguishes the two and only treats real drift as
    # contract regression.
    stripped_help = _strip_ansi(help_text).strip()
    raw_failures = _capability_failures(spec, help_text)
    total_required = len(spec.required_flags) + len(spec.required_subcommands)
    all_required_missing = total_required > 0 and len(raw_failures) == total_required
    if rc != 0 and (not stripped_help or all_required_missing):
        snippet = stripped_help[:300] or "<no output>"
        reason = "no output" if not stripped_help else "no required tokens advertised"
        result.runtime_failure = (
            f"`{' '.join(spec.resolved_help_command())}` exited {rc} with {reason}; "
            f"upstream CLI runtime failure, not contract drift: {snippet}"
        )
        return result

    result.capability_failures = raw_failures

    # 2. Optional model-presence check.
    if spec.models_required_present and spec.models_command:
        if spec.auth_required_for_models and not _secret_present(spec.auth_secret_env):
            # Coverage degrades to help-only; the workflow records this
            # so operators can decide whether to add the secret.
            result.skipped_reason = f"model check needs {spec.auth_secret_env}; running help-only"
        else:
            extra_env: dict[str, str] = {}
            if spec.auth_secret_env:
                value = os.environ.get(spec.auth_secret_env)
                if value is not None:
                    extra_env[spec.auth_secret_env] = value
            models_env = _sandbox_env(extra_env)
            rc_m, models_text = _run_capture(
                list(spec.models_command),
                timeout=_MODELS_TIMEOUT_SECONDS,
                env=models_env,
            )
            result.models_checked = rc_m == 0
            if rc_m != 0:
                result.model_failures.append(
                    f"`{' '.join(spec.models_command)}` exited {rc_m}: {models_text.strip()[:200]}"
                )
            else:
                result.model_failures = _model_failures(spec, models_text)

    return result


def list_contracts(contracts_dir: Path | None = None) -> list[str]:
    """Return the sorted list of adapter names with a contract on disk."""
    base = contracts_dir if contracts_dir is not None else CONTRACTS_DIR
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.yaml"))


# ---------------------------------------------------------------------------
# Per-adapter strategy enums (issue #1627)
# ---------------------------------------------------------------------------
#
# Every CLI agent expresses the same three concepts differently:
#
#   * resume        - ``--resume <id>`` for some, ``--session-id <id>`` for
#                     others, a subcommand ``<cli> resume <id>`` for a third
#                     group, or no native resume at all.
#   * dangerous mode - "skip permission prompts" is a flag here, an env var
#                     there, always-on for adapters with no permission system,
#                     and unsupported for the rest.
#   * event channel  - the surface Bernstein observes for lifecycle signals:
#                     stream-json, the canonical ``BERNSTEIN:<KIND>`` text
#                     grammar, upstream hooks, or PTY polling.
#
# Capturing each axis as a typed per-adapter enum compresses the scattered
# ``if adapter == "X"`` conditionals into one dispatch per axis and makes
# adding a new adapter a contract-completion exercise rather than a
# hunt-and-patch. Strategy is *declared* (see :data:`STRATEGY_MATRIX`); we do
# not probe the CLI at runtime.


class ResumeStrategy(StrEnum):
    """How an adapter reattaches to a prior session for ``bernstein resume``."""

    #: Single flag carrying the session id, e.g. ``--resume <id>``.
    FLAG = "flag"
    #: A pair of flags: one names the existing session, one mints a new one,
    #: e.g. ``--continue-from <old> --session-id <new>``.
    FLAG_PAIR = "flag-pair"
    #: A dedicated subcommand, e.g. ``<cli> resume <id>``.
    SUBCOMMAND = "subcommand"
    #: No native resume; the orchestrator falls back to a fresh session with
    #: scratchpad reinjection.
    UNSUPPORTED = "unsupported"


class DangerousModeStrategy(StrEnum):
    """How an adapter is told to skip interactive permission prompts."""

    #: A CLI flag, e.g. ``--yolo`` or ``--permission-mode bypassPermissions``.
    CLI_FLAG = "cli-flag"
    #: An environment variable the CLI reads at startup.
    ENV_VAR = "env-var"
    #: The CLI has no permission system; it is always non-interactive.
    ALWAYS_ON = "always-on"
    #: The CLI has no non-interactive mode and cannot be driven unattended
    #: in dangerous mode.
    UNSUPPORTED = "unsupported"


class EventChannel(StrEnum):
    """The surface Bernstein reads for an adapter's lifecycle signals."""

    #: Upstream emits newline-delimited JSON events (Claude/Cursor/Gemini).
    STREAM_JSON = "stream-json"
    #: Plain stdout carrying the canonical ``BERNSTEIN:<KIND>`` text grammar.
    TEXT_SIGNALS = "text-signals"
    #: Upstream fires hooks/callbacks Bernstein registers against.
    HOOKS = "hooks"
    #: No structured channel; Bernstein polls a PTY/log for liveness.
    POLL_PTY = "poll-pty"
    #: No event channel at all (process-exit detection only).
    NONE = "none"


class StrategyView(TypedDict):
    """JSON-serialisable view of an :class:`AdapterStrategy`'s three axes."""

    resume: str
    dangerous_mode: str
    event_channel: str


class StrategyRow(StrategyView):
    """A :class:`StrategyView` plus the adapter name, one row per adapter."""

    adapter: str


@dataclass(frozen=True)
class AdapterStrategy:
    """The declared strategy of a single adapter across all three axes."""

    resume: ResumeStrategy = ResumeStrategy.UNSUPPORTED
    dangerous_mode: DangerousModeStrategy = DangerousModeStrategy.UNSUPPORTED
    event_channel: EventChannel = EventChannel.TEXT_SIGNALS

    def to_dict(self) -> StrategyView:
        """Return a JSON-serialisable view for operator-facing tables."""
        return {
            "resume": str(self.resume),
            "dangerous_mode": str(self.dangerous_mode),
            "event_channel": str(self.event_channel),
        }


#: Default strategy applied to any adapter (built-in or third-party) absent
#: from :data:`STRATEGY_MATRIX`. Conservative on every axis so an undeclared
#: adapter never accidentally resumes natively or skips permissions.
DEFAULT_ADAPTER_STRATEGY = AdapterStrategy()


#: Per-adapter strategy declarations, keyed by registry name. Adding a new
#: adapter means adding a row here; the conformance harness
#: (:func:`undeclared_strategies`) reports any registry adapter missing a row.
STRATEGY_MATRIX: dict[str, AdapterStrategy] = {
    # Native session resume + structured event channel.
    "claude": AdapterStrategy(
        resume=ResumeStrategy.FLAG,
        dangerous_mode=DangerousModeStrategy.CLI_FLAG,
        event_channel=EventChannel.STREAM_JSON,
    ),
    "claude_routine": AdapterStrategy(
        resume=ResumeStrategy.FLAG,
        dangerous_mode=DangerousModeStrategy.CLI_FLAG,
        event_channel=EventChannel.STREAM_JSON,
    ),
    "openai_agents": AdapterStrategy(
        resume=ResumeStrategy.FLAG,
        dangerous_mode=DangerousModeStrategy.ALWAYS_ON,
        event_channel=EventChannel.HOOKS,
    ),
    # Stream-json adapters without native resume.
    "cursor": AdapterStrategy(
        resume=ResumeStrategy.UNSUPPORTED,
        dangerous_mode=DangerousModeStrategy.CLI_FLAG,
        event_channel=EventChannel.STREAM_JSON,
    ),
    "gemini": AdapterStrategy(
        resume=ResumeStrategy.UNSUPPORTED,
        dangerous_mode=DangerousModeStrategy.CLI_FLAG,
        event_channel=EventChannel.STREAM_JSON,
    ),
    # Antigravity is the upstream rename of the Gemini CLI binary
    # (transition deadline 2026-06-18 for free / Pro / Ultra). Same
    # strategy on every axis - it is the same adapter, only the
    # discovered binary name differs.
    "antigravity": AdapterStrategy(
        resume=ResumeStrategy.UNSUPPORTED,
        dangerous_mode=DangerousModeStrategy.CLI_FLAG,
        event_channel=EventChannel.STREAM_JSON,
    ),
    # agy is the successor CLI for the discontinued non-enterprise hosted
    # gemini backend; separate adapter (single binary, print mode, sandbox
    # pinned) -- see docs/adapters/agy.md. Dangerous mode is
    # --dangerously-skip-permissions; print mode emits plain text (no
    # structured event stream), so the channel is text signals. The CLI
    # has --conversation <id> resume, but native reattach is not wired
    # yet, so resume stays declared unsupported (fresh-session fallback).
    "agy": AdapterStrategy(
        resume=ResumeStrategy.UNSUPPORTED,
        dangerous_mode=DangerousModeStrategy.CLI_FLAG,
        event_channel=EventChannel.TEXT_SIGNALS,
    ),
    # CLI-flag dangerous mode, text-signal channel, fresh-session resume.
    "cline": AdapterStrategy(dangerous_mode=DangerousModeStrategy.CLI_FLAG),
    "charm": AdapterStrategy(dangerous_mode=DangerousModeStrategy.CLI_FLAG),
    "kimi": AdapterStrategy(dangerous_mode=DangerousModeStrategy.CLI_FLAG),
    "rovo": AdapterStrategy(dangerous_mode=DangerousModeStrategy.CLI_FLAG),
    "letta_code": AdapterStrategy(dangerous_mode=DangerousModeStrategy.CLI_FLAG),
    # Codex drives unattended via its sandbox/full-auto flag.
    "codex": AdapterStrategy(dangerous_mode=DangerousModeStrategy.CLI_FLAG),
    # Everyone else - no native resume, text-signal channel. Dangerous-mode
    # default is ``UNSUPPORTED`` until an adapter declares otherwise.
    "aichat": AdapterStrategy(),
    "aider": AdapterStrategy(),
    "amp": AdapterStrategy(),
    "auggie": AdapterStrategy(),
    "autohand": AdapterStrategy(),
    "clm": AdapterStrategy(),
    "cloudflare": AdapterStrategy(event_channel=EventChannel.HOOKS),
    "codebuff": AdapterStrategy(),
    "cody": AdapterStrategy(),
    "composio": AdapterStrategy(event_channel=EventChannel.HOOKS),
    "continue": AdapterStrategy(),
    # Copilot drives unattended via --allow-all-tools / --no-ask-user in print
    # mode; the deterministic session id is pinned through --session-id.
    "copilot": AdapterStrategy(dangerous_mode=DangerousModeStrategy.CLI_FLAG),
    "devin_terminal": AdapterStrategy(event_channel=EventChannel.POLL_PTY),
    "droid": AdapterStrategy(),
    "forge": AdapterStrategy(),
    "generic": AdapterStrategy(),
    "goose": AdapterStrategy(),
    "gptme": AdapterStrategy(),
    "hermes": AdapterStrategy(),
    "iac": AdapterStrategy(),
    "junie": AdapterStrategy(),
    "kilo": AdapterStrategy(),
    "kiro": AdapterStrategy(),
    "mistral": AdapterStrategy(),
    "mock": AdapterStrategy(),
    "ollama": AdapterStrategy(),
    "open_interpreter": AdapterStrategy(),
    "opencode": AdapterStrategy(),
    "openhands": AdapterStrategy(),
    "pi": AdapterStrategy(),
    "plandex": AdapterStrategy(),
    "q_dev": AdapterStrategy(),
    "qwen": AdapterStrategy(),
    "ralphex": AdapterStrategy(),
}


#: Maps the session-namespace form of an adapter (the lower-cased
#: :meth:`CLIAdapter.name`) to its registry key, for the adapters whose
#: human-readable name does not match the key the matrix is declared under.
#: This keeps :meth:`CLIAdapter.strategy` free of any registry import (which
#: would break the ``adapters-independent`` import-linter contract) while
#: still resolving the correct row. Adapters whose ``name()`` already lowers
#: to their registry key need no entry here.
_NAMESPACE_ALIASES: dict[str, str] = {
    "claude code": "claude",
    "cloudflare agents": "cloudflare",
    "composio agent orchestrator": "composio",
    "continue.dev": "continue",
    "github copilot": "copilot",
    "generic cli": "generic",
    "hermes agent": "hermes",
    "iac (terraform/pulumi)": "iac",
    "letta code": "letta_code",
    "mistral vibe": "mistral",
    "ollama (local)": "ollama",
    "open interpreter": "open_interpreter",
    "openai agents sdk": "openai_agents",
    "qwen cli": "qwen",
    "rovo dev": "rovo",
}


def strategy_for(adapter_name: str) -> AdapterStrategy:
    """Return the declared :class:`AdapterStrategy` for ``adapter_name``.

    Accepts either a registry key (``"claude"``) or the session-namespace
    form (``"claude code"``); the latter is mapped through
    :data:`_NAMESPACE_ALIASES` first. Unknown adapters fall back to
    :data:`DEFAULT_ADAPTER_STRATEGY`, which is conservative on every axis (no
    native resume, dangerous mode unsupported, text-signal event channel).
    """
    key = _NAMESPACE_ALIASES.get(adapter_name, adapter_name)
    return STRATEGY_MATRIX.get(key, DEFAULT_ADAPTER_STRATEGY)


def undeclared_strategies(adapter_names: list[str]) -> list[str]:
    """Return the subset of ``adapter_names`` with no row in the matrix.

    The conformance harness passes the registry's adapter names; a non-empty
    result is a hard failure (issue #1627 AC #2): every shipped adapter must
    declare its strategy on each axis.
    """
    return sorted(name for name in adapter_names if name not in STRATEGY_MATRIX)


def strategy_table(adapter_names: list[str] | None = None) -> list[StrategyRow]:
    """Return one row per adapter for the operator-facing strategy table.

    Each row is a :class:`StrategyRow` (``adapter`` plus the three axes).
    Rows are sorted by adapter name so operators can compare adapters at a
    glance (issue #1627 AC #4). When ``adapter_names`` is ``None`` the full
    matrix is rendered.
    """
    names = sorted(adapter_names) if adapter_names is not None else sorted(STRATEGY_MATRIX)
    rows: list[StrategyRow] = []
    for name in names:
        row: StrategyRow = {"adapter": name, **strategy_for(name).to_dict()}
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Resume-capability back-compat shim (feat-resume-from-checkpoint)
# ---------------------------------------------------------------------------
#
# The resume axis used to be a standalone two-state string matrix. It is now
# derived from :data:`STRATEGY_MATRIX` so there is a single source of truth.
# The string constants and :func:`resume_capability` are retained verbatim so
# ``bernstein resume`` and the lifecycle env var keep their stable contract.

#: Adapter has no native resume; the CLI falls back to a fresh session.
RESUME_FALLBACK_FRESH: str = "fallback-fresh"

#: Adapter reattaches to the prior session via a provider-side session id.
RESUME_NATIVE: str = "native"


def resume_capability(adapter_name: str) -> str:
    """Return the legacy two-state resume capability for ``adapter_name``.

    Derived from :data:`STRATEGY_MATRIX`: any :class:`ResumeStrategy` other
    than :attr:`ResumeStrategy.UNSUPPORTED` maps to :data:`RESUME_NATIVE`.
    Unknown adapters default to :data:`RESUME_FALLBACK_FRESH`.
    """
    strategy = strategy_for(adapter_name)
    if strategy.resume is ResumeStrategy.UNSUPPORTED:
        return RESUME_FALLBACK_FRESH
    return RESUME_NATIVE


#: Legacy two-state view of the resume axis rendered as ``adapter ->
#: capability``. Derived from :data:`STRATEGY_MATRIX` for back-compat with
#: callers that imported the dict directly. Adapters absent are assumed
#: :data:`RESUME_FALLBACK_FRESH`.
RESUME_CAPABILITY_MATRIX: dict[str, str] = {name: resume_capability(name) for name in STRATEGY_MATRIX}


# ---------------------------------------------------------------------------
# Checkpointed-retry capability map (issue #2359)
# ---------------------------------------------------------------------------
#
# A failed task's retry can continue the prior native agent session (warm),
# branch a new session off the recorded checkpoint (fork), or restart from
# zero (cold). What a given adapter can offer is a pure function of the
# resume axis it already declares in :data:`STRATEGY_MATRIX` -- a single
# source of truth, so the retry surface can never claim a warm resume that
# ``bernstein resume`` would refuse.


class CheckpointRetryCapability(StrEnum):
    """What kind of checkpointed retry an adapter's native sessions support."""

    #: The adapter can reattach to the prior session (warm continuation).
    RESUME = "resume"
    #: The adapter can additionally branch a fresh session off a recorded
    #: checkpoint, leaving the original session intact.
    FORK = "fork"
    #: No native session continuation; every retry is a cold restart.
    NONE = "none"


#: Registry keys of adapters whose native session store supports branching a
#: new session from an existing one (a superset of plain resume). Kept as an
#: explicit declaration: fork support is a stronger upstream contract than
#: the resume flag alone proves.
_FORK_CAPABLE_ADAPTERS: frozenset[str] = frozenset({"claude", "claude_routine"})


def checkpoint_retry_capability(adapter_name: str) -> CheckpointRetryCapability:
    """Return the checkpointed-retry capability for ``adapter_name``.

    Derived from :data:`STRATEGY_MATRIX`: an adapter whose declared
    :class:`ResumeStrategy` is :attr:`ResumeStrategy.UNSUPPORTED` can never
    be warm/fork capable (:attr:`CheckpointRetryCapability.NONE`). Adapters
    with native resume are :attr:`CheckpointRetryCapability.RESUME`, upgraded
    to :attr:`CheckpointRetryCapability.FORK` when the adapter is in the
    explicit fork-capable set. Unknown adapters degrade to ``NONE`` so an
    undeclared adapter never accidentally resumes a provider-side session.
    """
    key = _NAMESPACE_ALIASES.get(adapter_name, adapter_name)
    strategy = strategy_for(key)
    if strategy.resume is ResumeStrategy.UNSUPPORTED:
        return CheckpointRetryCapability.NONE
    if key in _FORK_CAPABLE_ADAPTERS:
        return CheckpointRetryCapability.FORK
    return CheckpointRetryCapability.RESUME


#: Full per-adapter checkpointed-retry capability map, one row per declared
#: adapter. Derived, never hand-maintained: a new adapter picks up its row
#: from the strategy matrix it must already declare.
CHECKPOINT_RETRY_CAPABILITY_MATRIX: dict[str, CheckpointRetryCapability] = {
    name: checkpoint_retry_capability(name) for name in STRATEGY_MATRIX
}


# ---------------------------------------------------------------------------
# In-process verification-gate capability map (issue #2360)
# ---------------------------------------------------------------------------
#
# An adapter with a *blocking* in-session hook surface can run our completion
# verification and path allowlist the moment a worker believes it is done,
# refusing the cheap miss before the turn ends. The scheduler-side evidence
# gate stays authoritative regardless -- the in-process gate is defence in
# depth -- so an adapter without such a surface simply degrades to that gate
# with no policy weakening. Capability is a stronger upstream contract than the
# event channel alone proves, so it is an explicit declaration.


class InProcessGateCapability(StrEnum):
    """Whether an adapter can enforce a verification gate in-session."""

    #: The adapter exposes a blocking hook surface (a completion gate that can
    #: refuse to end the turn plus a tool-permission matcher that can refuse an
    #: out-of-scope write) Bernstein renders its policy into.
    BLOCKING = "blocking"
    #: No blocking hook surface; the gate runs scheduler-side only.
    NONE = "none"


#: Registry keys of adapters that drive the Claude Code hook surface (settings
#: ``hooks`` with PreToolUse permission decisions and a blocking Stop hook).
#: Both entries are Claude Code driver adapters -- the second (``claude_routine``)
#: reuses the identical surface in routine/scheduled mode -- so the renderer
#: handles the family uniformly.
_IN_PROCESS_GATE_BLOCKING_ADAPTERS: frozenset[str] = frozenset({"claude", "claude_routine"})


def in_process_gate_capability(adapter_name: str) -> InProcessGateCapability:
    """Return the in-process gate capability for ``adapter_name``.

    Accepts a registry key or the session-namespace form (resolved through
    :data:`_NAMESPACE_ALIASES`). Adapters in the explicit blocking set map to
    :attr:`InProcessGateCapability.BLOCKING`; every other adapter -- declared or
    unknown -- degrades to :attr:`InProcessGateCapability.NONE` so an
    undeclared adapter never claims an in-session enforcement surface it lacks.
    """
    key = _NAMESPACE_ALIASES.get(adapter_name, adapter_name)
    if key in _IN_PROCESS_GATE_BLOCKING_ADAPTERS:
        return InProcessGateCapability.BLOCKING
    return InProcessGateCapability.NONE


#: Full per-adapter in-process gate capability map, one row per declared
#: adapter. Derived, never hand-maintained: a new adapter picks up its row from
#: the strategy matrix it must already declare.
IN_PROCESS_GATE_CAPABILITY_MATRIX: dict[str, InProcessGateCapability] = {
    name: in_process_gate_capability(name) for name in STRATEGY_MATRIX
}


# ---------------------------------------------------------------------------
# Cost-aware scheduling capability maps (issue #2354)
# ---------------------------------------------------------------------------
#
# Cost-aware scheduling routes non-interactive work to a provider's batch
# surface and schedules cache-window fan-outs. Both are provider contracts an
# adapter either honours or does not; like the strategy matrix, they are
# *declared* here as the single source of truth, never probed at runtime, so
# the scheduler can never route batch work to an adapter that has no batch
# endpoint or assume a prompt-cache TTL an adapter does not offer. The default
# on every axis is the conservative one (no batch surface, no cache window),
# so an undeclared or third-party adapter is never routed unsafely.


class BatchDispatchCapability(StrEnum):
    """Whether an adapter exposes a non-interactive batch dispatch surface."""

    #: The adapter routes through a provider batch endpoint (large discount,
    #: asynchronous, non-interactive) for batch-eligible work.
    NATIVE = "native"
    #: No batch surface; every call is dispatched interactively.
    NONE = "none"


#: Registry keys of adapters whose priced upstream exposes a batch API that
#: Bernstein dispatches through. Kept explicit: a batch surface is a stronger
#: provider contract than the interactive path and must be declared, not
#: inferred from the model string. Conservative by construction -- an adapter
#: absent here is treated as non-batch.
_BATCH_CAPABLE_ADAPTERS: frozenset[str] = frozenset(
    {
        "claude",
        "claude_routine",
        "openai_agents",
    }
)


def batch_dispatch_capability(adapter_name: str) -> BatchDispatchCapability:
    """Return the batch-dispatch capability for ``adapter_name``.

    Accepts either a registry key or the session-namespace form (resolved
    through :data:`_NAMESPACE_ALIASES` first). An adapter in
    :data:`_BATCH_CAPABLE_ADAPTERS` reports
    :attr:`BatchDispatchCapability.NATIVE`; every other adapter -- including
    unknown / third-party adapters -- reports
    :attr:`BatchDispatchCapability.NONE`, so batch-eligible work is never
    routed to an adapter that cannot honour it.
    """
    key = _NAMESPACE_ALIASES.get(adapter_name, adapter_name)
    if key in _BATCH_CAPABLE_ADAPTERS:
        return BatchDispatchCapability.NATIVE
    return BatchDispatchCapability.NONE


#: Full per-adapter batch-dispatch capability map, one row per declared
#: adapter. Derived from :data:`_BATCH_CAPABLE_ADAPTERS`; a new adapter picks
#: up its row automatically from the strategy matrix it must already declare.
BATCH_DISPATCH_CAPABILITY_MATRIX: dict[str, BatchDispatchCapability] = {
    name: batch_dispatch_capability(name) for name in STRATEGY_MATRIX
}


class CacheWindowCapability(StrEnum):
    """Whether an adapter honours a prompt-cache TTL window for fan-out."""

    #: The adapter's upstream caches a shared prompt prefix under a short TTL,
    #: so a single warm-up call primes the cache for a fan-out of workers that
    #: share the prefix.
    SUPPORTED = "supported"
    #: No documented prompt-cache window; fan-out warm-up is a no-op and the
    #: scheduler must not assume cache hits.
    NONE = "none"


#: Registry keys of adapters whose upstream documents a prompt-cache TTL
#: window Bernstein can schedule a fan-out inside. Explicit and conservative:
#: an adapter absent here has no cache window, so the scheduler never issues a
#: warm-up call expecting hits that will not happen.
_CACHE_WINDOW_ADAPTERS: frozenset[str] = frozenset(
    {
        "claude",
        "claude_routine",
    }
)


def cache_window_capability(adapter_name: str) -> CacheWindowCapability:
    """Return the cache-window capability for ``adapter_name``.

    Accepts either a registry key or the session-namespace form. An adapter in
    :data:`_CACHE_WINDOW_ADAPTERS` reports
    :attr:`CacheWindowCapability.SUPPORTED`; every other adapter reports
    :attr:`CacheWindowCapability.NONE`. Note the capability only says an
    adapter *can* honour a cache window -- the scheduler still requires an
    explicit opt-in (conservative default off) before it issues a warm-up.
    """
    key = _NAMESPACE_ALIASES.get(adapter_name, adapter_name)
    if key in _CACHE_WINDOW_ADAPTERS:
        return CacheWindowCapability.SUPPORTED
    return CacheWindowCapability.NONE


#: Full per-adapter cache-window capability map, one row per declared adapter.
CACHE_WINDOW_CAPABILITY_MATRIX: dict[str, CacheWindowCapability] = {
    name: cache_window_capability(name) for name in STRATEGY_MATRIX
}
