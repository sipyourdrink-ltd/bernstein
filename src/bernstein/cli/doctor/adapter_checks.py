"""Adapter binary reachability checks.

For every adapter referenced in ``bernstein.yaml`` (or, when no config is
available, the default short-list), ``check_adapter_binary`` resolves the
binary on PATH with :func:`shutil.which` and shells out
``<binary> --version`` with a short timeout. The result is reported as a
:class:`DoctorResult` so the renderer can show version, missing-binary
and hung-binary cases side by side.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, cast

from bernstein.cli.doctor.report import DoctorResult

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


# Default mapping from adapter name to the binary the adapter spawns.
# Kept in sync with src/bernstein/adapters/*.py. The mapping is exposed
# publicly so callers can override or extend it.
ADAPTER_BINARIES: dict[str, str] = {
    "agy": "agy",
    "aichat": "aichat",
    "aider": "aider",
    "amp": "amp",
    "auggie": "auggie",
    "autohand": "autohand",
    "charm": "crush",
    "claude": "claude",
    "cline": "cline",
    "codebuff": "codebuff",
    "codex": "codex",
    "cody": "cody",
    "composio": "ao",
    "continue": "continue",
    "copilot": "gh",
    "cursor": "cursor-agent",
    "devin_terminal": "devin",
    "droid": "droid",
    "forge": "forge",
    "gemini": "gemini",
    "goose": "goose",
    "gptme": "gptme",
    "hermes": "hermes",
    "iac": "terraform",
    "junie": "junie",
    "kilo": "kilo",
    "kimi": "kimi",
    "kiro": "kiro",
    "letta_code": "letta",
    "mistral": "mistral",
    "ollama": "ollama",
    "open_interpreter": "interpreter",
    "opencode": "opencode",
    "openhands": "openhands",
    "pi": "pi",
    "plandex": "plandex",
    "q_dev": "q",
    "qwen": "qwen",
    "ralphex": "ralphex",
    "rovo": "rovo",
}


_VERSION_TIMEOUT_SECONDS = 5.0


async def check_adapter_binary(
    adapter_name: str,
    declared_binary: str,
    *,
    timeout: float = _VERSION_TIMEOUT_SECONDS,
) -> DoctorResult:
    """Check that the declared binary is on PATH and responds to ``--version``.

    Args:
        adapter_name: Adapter identifier as used in ``bernstein.yaml``.
        declared_binary: Executable to look up via :func:`shutil.which`.
        timeout: How long to wait for ``--version`` before warning.

    Returns:
        DoctorResult with status ``ok`` (version captured), ``warn``
        (binary present but hung or exited non-zero), or ``fail``
        (binary missing).
    """
    name = f"adapter:{adapter_name}"
    if not declared_binary:
        return DoctorResult(
            name=name,
            category="adapter",
            status="fail",
            detail="no binary declared for adapter",
            remediation=f"Add a binary mapping for `{adapter_name}` or remove it from bernstein.yaml",
        )

    path = shutil.which(declared_binary)
    if path is None:
        return DoctorResult(
            name=name,
            category="adapter",
            status="fail",
            detail=f"Binary `{declared_binary}` not in PATH",
            remediation=(
                f"Install via the adapter's vendor instructions or remove `{adapter_name}` from bernstein.yaml"
            ),
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            declared_binary,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, asyncio.CancelledError) as exc:  # pragma: no cover - rare
        return DoctorResult(
            name=name,
            category="adapter",
            status="warn",
            detail=f"failed to spawn `{declared_binary} --version`: {exc}",
            remediation="Verify the binary is executable",
        )

    try:
        out_bytes, err_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _terminate_and_wait(proc)
        return DoctorResult(
            name=name,
            category="adapter",
            status="warn",
            detail=f"`{declared_binary} --version` timed out after {timeout:g}s",
            remediation="Adapter binary may be wedged; try running it directly",
        )

    out = out_bytes.decode("utf-8", errors="replace").strip()
    err = err_bytes.decode("utf-8", errors="replace").strip()
    version = _first_nonempty_line(out) or _first_nonempty_line(err) or "version output empty"

    if proc.returncode and proc.returncode != 0:
        return DoctorResult(
            name=name,
            category="adapter",
            status="warn",
            detail=f"`{declared_binary} --version` exited {proc.returncode}: {version}",
            remediation="Reinstall or upgrade the adapter binary",
        )

    return DoctorResult(
        name=name,
        category="adapter",
        status="ok",
        detail=f"{path} -> {version}",
    )


async def run_adapter_checks(
    adapter_names: Iterable[str] | None = None,
    *,
    binaries: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> list[DoctorResult]:
    """Run binary checks for the requested adapters in parallel.

    When ``adapter_names`` is ``None``, the list is loaded from
    ``bernstein.yaml`` (or falls back to the curated default of common
    adapters). Unknown adapters are still checked - the report will mark
    them missing rather than silently skipping.
    """
    table = dict(binaries) if binaries is not None else ADAPTER_BINARIES.copy()
    names = (
        list(adapter_names)
        if adapter_names is not None
        else _load_adapters_from_yaml(config_path) or _default_adapter_list()
    )
    if not names:
        return [
            DoctorResult(
                name="adapter:none",
                category="adapter",
                status="skip",
                detail="no adapters configured",
                remediation="Add an `agents:` section to bernstein.yaml",
            )
        ]

    coros = [check_adapter_binary(n, table.get(n, n)) for n in names]
    return list(await asyncio.gather(*coros))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _terminate_and_wait(proc: asyncio.subprocess.Process) -> None:
    """Best-effort terminate plus wait so transports close cleanly.

    Without the explicit wait, asyncio occasionally emits a stray
    "Event loop is closed" warning when the subprocess transport is
    finalised after pytest has already torn the loop down.
    """
    try:
        proc.kill()
    except ProcessLookupError:  # pragma: no cover - race
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except (TimeoutError, ProcessLookupError):  # pragma: no cover - defensive
        return


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _default_adapter_list() -> list[str]:
    """Fallback adapter list when no project config is available."""
    return ["claude", "codex", "gemini", "qwen", "aider"]


def _load_adapters_from_yaml(config_path: Path | None) -> list[str]:
    """Best-effort scrape of adapter names from bernstein.yaml.

    The function is intentionally tolerant of missing config or malformed
    YAML - the doctor must never crash because the user's config is
    broken. Returns an empty list when nothing can be parsed.
    """
    path = config_path or (Path.cwd() / "bernstein.yaml")
    if not path.is_file():
        return []

    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML always present in prod
        return []

    try:
        with path.open(encoding="utf-8") as fh:
            raw: object = yaml.safe_load(fh)
    except Exception:
        return []

    if not isinstance(raw, dict):
        return []

    return _extract_adapter_names(cast("dict[str, object]", raw))


def _extract_adapter_names(raw: dict[str, object]) -> list[str]:
    """Pure extractor for adapter names from a parsed YAML mapping."""
    seen: list[str] = []
    seen_set: set[str] = set()

    def _add(candidate: object) -> None:
        if isinstance(candidate, str) and candidate and candidate not in seen_set:
            seen.append(candidate)
            seen_set.add(candidate)

    _add(raw.get("cli"))

    adapters_raw = raw.get("adapters")
    if isinstance(adapters_raw, list):
        items_list = cast("list[object]", adapters_raw)
        for item in items_list:
            if isinstance(item, str):
                _add(item)
            elif isinstance(item, dict):
                item_dict = cast("dict[str, object]", item)
                _add(item_dict.get("name") or item_dict.get("cli"))
    elif isinstance(adapters_raw, dict):
        adapters_map = cast("dict[str, object]", adapters_raw)
        for key in adapters_map:
            _add(key)

    role_policy = raw.get("role_model_policy")
    if isinstance(role_policy, dict):
        policy_map = cast("dict[str, object]", role_policy)
        for entry in policy_map.values():
            if isinstance(entry, dict):
                entry_map = cast("dict[str, object]", entry)
                _add(entry_map.get("cli"))

    return seen
