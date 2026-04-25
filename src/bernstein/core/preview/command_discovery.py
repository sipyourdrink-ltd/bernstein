"""Auto-discovery of dev-server commands for ``bernstein preview``.

The acceptance criteria fix the precedence:

1. ``package.json :: scripts.dev`` (then ``scripts.start``).
2. ``Procfile`` — first ``web:`` line wins, otherwise the first process.
3. ``.tool-versions`` — best-effort hint that surfaces nothing on its own
   but is collected so ``preview start --list-commands`` can show the
   user which runtime is pinned.
4. ``bernstein.yaml :: preview.command``.

``discover_commands`` returns the first match. ``list_candidates`` returns
every candidate the discovery saw, in declaration order, so callers can
expose them under ``preview start --list-commands``.

Discovery is deliberately I/O-only and side-effect free — no subprocess
calls. The caller decides what to execute.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredCommand:
    """A candidate dev-server command surfaced by discovery.

    Attributes:
        source: Human-readable origin of the command — one of
            ``"package.json:dev"``, ``"package.json:start"``,
            ``"Procfile:web"``, ``"Procfile:<name>"``,
            ``".tool-versions"``, ``"bernstein.yaml"``.
        command: The exact shell-quoted command to execute. ``None`` for
            sources (like ``.tool-versions``) that record metadata but
            don't yield an executable command.
        details: Free-form annotations — for instance the runtime name
            for ``.tool-versions`` entries.
    """

    source: str
    command: str | None
    details: str = ""

    def is_runnable(self) -> bool:
        """Return ``True`` when the candidate has a non-empty command."""
        return bool(self.command and self.command.strip())


def _parse_package_json(path: Path) -> list[DiscoveredCommand]:
    """Extract ``scripts.dev`` and ``scripts.start`` from a ``package.json``.

    Args:
        path: Path to the ``package.json`` file.

    Returns:
        A list (length 0-2) ordered ``dev`` first, ``start`` second. If
        the file is unreadable or malformed an empty list is returned;
        discovery never raises on bad input.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("package.json read failed for %s: %s", path, exc)
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.debug("package.json parse failed for %s: %s", path, exc)
        return []
    if not isinstance(data, dict):
        return []
    scripts_obj = data.get("scripts")
    if not isinstance(scripts_obj, dict):
        return []
    found: list[DiscoveredCommand] = []
    for key in ("dev", "start"):
        value = scripts_obj.get(key)
        if isinstance(value, str) and value.strip():
            # `npm run` is the canonical way to invoke a package script.
            # `bun` / `pnpm` / `yarn` users can override via --command.
            found.append(
                DiscoveredCommand(
                    source=f"package.json:{key}",
                    command=f"npm run {key}",
                    details=value.strip(),
                )
            )
    return found


def _parse_procfile(path: Path) -> list[DiscoveredCommand]:
    """Extract process commands from a Heroku-style ``Procfile``.

    Returns processes in declaration order. ``web`` is annotated but not
    re-ordered — the first ``web:`` line wins because we walk in order
    and discovery's auto-pick chooses the first runnable command.

    Args:
        path: Path to the ``Procfile``.

    Returns:
        A list of :class:`DiscoveredCommand` entries, one per line.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("Procfile read failed for %s: %s", path, exc)
        return []
    found: list[DiscoveredCommand] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, cmd = stripped.partition(":")
        if not sep:
            continue
        name = name.strip()
        cmd = cmd.strip()
        if not name or not cmd:
            continue
        found.append(
            DiscoveredCommand(
                source=f"Procfile:{name}",
                command=cmd,
            )
        )
    # Sort so a "web" entry surfaces first even if declared later — a
    # web preview is the dominant use-case.
    found.sort(key=lambda c: 0 if c.source == "Procfile:web" else 1)
    return found


def _parse_tool_versions(path: Path) -> list[DiscoveredCommand]:
    """Surface ``.tool-versions`` runtimes for visibility only.

    ``.tool-versions`` doesn't contain a runnable command — it just
    records the runtime versions an asdf-managed project pins. We list
    them so ``preview start --list-commands`` can show the user that the
    project pins, e.g., ``nodejs 20.10.0``.

    Args:
        path: Path to a ``.tool-versions`` file.

    Returns:
        One :class:`DiscoveredCommand` per non-empty line; ``command``
        is always ``None``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug(".tool-versions read failed for %s: %s", path, exc)
        return []
    found: list[DiscoveredCommand] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        found.append(
            DiscoveredCommand(
                source=".tool-versions",
                command=None,
                details=stripped,
            )
        )
    return found


def _parse_bernstein_yaml(path: Path) -> list[DiscoveredCommand]:
    """Read ``bernstein.yaml :: preview.command`` if present.

    Args:
        path: Path to a ``bernstein.yaml`` (or ``bernstein.yml``) file.

    Returns:
        A single-entry list when the key exists and is a non-empty
        string, else an empty list. PyYAML failures are demoted to debug
        logs.
    """
    try:
        # Local import: yaml is a soft dependency for the rest of the
        # package, so importing it lazily keeps preview discovery cheap
        # in environments where the file doesn't exist anyway.
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - PyYAML is a hard dep
        logger.debug("PyYAML unavailable, skipping bernstein.yaml: %s", exc)
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("bernstein.yaml read failed for %s: %s", path, exc)
        return []
    try:
        data = yaml.safe_load(text)  # type: ignore[no-untyped-call]
    except yaml.YAMLError as exc:
        logger.debug("bernstein.yaml parse failed for %s: %s", path, exc)
        return []
    if not isinstance(data, dict):
        return []
    preview_section = data.get("preview")
    if not isinstance(preview_section, dict):
        return []
    cmd = preview_section.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return []
    return [
        DiscoveredCommand(
            source="bernstein.yaml",
            command=cmd.strip(),
        )
    ]


def list_candidates(cwd: Path) -> list[DiscoveredCommand]:
    """List every dev-server candidate under *cwd* in precedence order.

    The order matches the ticket's acceptance criteria:
    ``package.json`` → ``Procfile`` → ``.tool-versions`` →
    ``bernstein.yaml``.

    Args:
        cwd: Project root to scan.

    Returns:
        Every :class:`DiscoveredCommand` found, in precedence order.
        Non-runnable entries (``.tool-versions``) are included so the
        ``--list-commands`` UI can show them — callers should call
        :meth:`DiscoveredCommand.is_runnable` to filter.
    """
    cwd = cwd.resolve()
    out: list[DiscoveredCommand] = []
    pkg = cwd / "package.json"
    if pkg.is_file():
        out.extend(_parse_package_json(pkg))
    proc = cwd / "Procfile"
    if proc.is_file():
        out.extend(_parse_procfile(proc))
    tv = cwd / ".tool-versions"
    if tv.is_file():
        out.extend(_parse_tool_versions(tv))
    for name in ("bernstein.yaml", "bernstein.yml"):
        cfg = cwd / name
        if cfg.is_file():
            out.extend(_parse_bernstein_yaml(cfg))
            break
    return out


def discover_commands(cwd: Path) -> DiscoveredCommand | None:
    """Return the first runnable command discovered under *cwd*.

    Implements the precedence described in the module docstring.

    Args:
        cwd: Project root.

    Returns:
        The first :class:`DiscoveredCommand` whose
        :meth:`~DiscoveredCommand.is_runnable` returns ``True``, or
        ``None`` when no runnable command was found.
    """
    for candidate in list_candidates(cwd):
        if candidate.is_runnable():
            return candidate
    return None


__all__ = [
    "DiscoveredCommand",
    "discover_commands",
    "list_candidates",
]
