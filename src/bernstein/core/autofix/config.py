"""Typed configuration reader for the autofix daemon.

The autofix daemon reads its watch-list and per-repo policy from
``~/.config/bernstein/autofix.toml``.  The format is intentionally
small so operators can hand-edit it without learning a schema:

.. code-block:: toml

    poll_interval_seconds = 60
    log_byte_budget = 65536

    [[repo]]
    name = "chernistry/bernstein"
    cost_cap_usd = 5.0
    allow_force_push = false
    label = "bernstein-autofix"

    [[repo]]
    name = "chernistry/example"
    cost_cap_usd = 2.0

The :func:`load_config` helper applies defaults for any field omitted
by the operator and returns a typed :class:`AutofixConfig` so the rest
of the package never touches raw dicts.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: Default file path consulted when the caller does not pass an explicit
#: ``path`` argument to :func:`load_config`.  Honours ``$XDG_CONFIG_HOME``.
DEFAULT_CONFIG_FILENAME = "autofix.toml"

#: Default poll interval in seconds when the operator omits the field.
DEFAULT_POLL_INTERVAL_SECONDS: int = 60

#: Default byte budget applied to ``gh run view --log-failed`` output
#: before goal synthesis.  Large enough to capture a full pytest
#: traceback, small enough to keep follow-up prompt budgets predictable.
DEFAULT_LOG_BYTE_BUDGET: int = 65_536

#: Default per-repo cost cap (USD).  Zero means "unlimited" — operators
#: typically configure a non-zero value.
DEFAULT_COST_CAP_USD: float = 5.0

#: Default label that gates whether the daemon may touch a PR.  The
#: operator opts in by adding this label to a PR.
DEFAULT_LABEL = "bernstein-autofix"

#: Hard cap on attempts per (PR, push SHA) tuple.  Beyond this, the
#: daemon hands off to a human via the ``needs-human`` label.
MAX_ATTEMPTS_PER_PUSH: int = 3


def _default_config_path() -> Path:
    """Return ``~/.config/bernstein/autofix.toml`` (XDG-aware)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "bernstein" / DEFAULT_CONFIG_FILENAME


# ---------------------------------------------------------------------------
# Typed config dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoConfig:
    """Per-repository autofix policy.

    Attributes:
        name: Fully-qualified ``owner/repo`` identifier.
        cost_cap_usd: Hard cap on spend per attempt; zero means
            unlimited.  The dispatcher consults this before invoking
            the bandit router so a runaway repo cannot drain the
            account.
        label: GitHub label that gates whether the daemon touches a PR.
            Defaults to :data:`DEFAULT_LABEL`.
        allow_force_push: Whether the dispatcher may force-push the
            attempt commit on the PR branch.  When ``False`` the daemon
            falls back to a merge commit on the branch tip.
    """

    name: str
    cost_cap_usd: float = DEFAULT_COST_CAP_USD
    label: str = DEFAULT_LABEL
    allow_force_push: bool = False


@dataclass(frozen=True)
class AutofixConfig:
    """Top-level autofix daemon configuration.

    Attributes:
        poll_interval_seconds: How often the daemon scans configured
            repos for failed CI runs.  Lower values respond faster but
            burn GitHub API quota.
        log_byte_budget: Maximum bytes of failing-log text passed to
            the classifier and the synthesised goal.  Larger logs are
            head-truncated.
        repos: Tuple of per-repo policies in the order they were
            declared; the daemon iterates this list every tick.
    """

    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    log_byte_budget: int = DEFAULT_LOG_BYTE_BUDGET
    repos: tuple[RepoConfig, ...] = field(default_factory=tuple)

    def repo(self, name: str) -> RepoConfig | None:
        """Return the :class:`RepoConfig` for ``name``, or ``None``."""
        for repo in self.repos:
            if repo.name == name:
                return repo
        return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _coerce_repo(raw: dict[str, Any]) -> RepoConfig:
    """Convert a raw TOML table into a :class:`RepoConfig`.

    Args:
        raw: The decoded dict for a single ``[[repo]]`` entry.

    Returns:
        A populated :class:`RepoConfig`; missing fields fall back to
        the module-level defaults.

    Raises:
        ValueError: If ``name`` is missing or empty — every repo entry
            must have an explicit identifier.
    """
    name = str(raw.get("name", "")).strip()
    if not name:
        raise ValueError("Each [[repo]] entry must declare a non-empty 'name'.")

    try:
        cost_cap = float(raw.get("cost_cap_usd", DEFAULT_COST_CAP_USD))
    except (TypeError, ValueError):
        cost_cap = DEFAULT_COST_CAP_USD
    if cost_cap < 0:
        cost_cap = 0.0

    label = str(raw.get("label", DEFAULT_LABEL)).strip() or DEFAULT_LABEL
    allow_force_push = bool(raw.get("allow_force_push", False))

    return RepoConfig(
        name=name,
        cost_cap_usd=cost_cap,
        label=label,
        allow_force_push=allow_force_push,
    )


def _coerce_repos(raw_repos: object) -> tuple[RepoConfig, ...]:
    """Convert the ``repo`` array of tables into a tuple of typed repos."""
    if not isinstance(raw_repos, list):
        return ()
    out: list[RepoConfig] = []
    for entry in raw_repos:  # type: ignore[reportUnknownVariableType]
        if not isinstance(entry, dict):
            continue
        # Re-key to satisfy strict typing.
        typed: dict[str, Any] = {str(k): v for k, v in entry.items()}  # type: ignore[reportUnknownVariableType]
        out.append(_coerce_repo(typed))
    return tuple(out)


def load_config(path: Path | None = None) -> AutofixConfig:
    """Load and validate the autofix daemon config from disk.

    Args:
        path: Explicit override; defaults to
            ``$XDG_CONFIG_HOME/bernstein/autofix.toml`` (or the XDG
            default).

    Returns:
        A populated :class:`AutofixConfig`.  When the file is absent
        an empty config (no repos) is returned so callers can show a
        helpful message instead of crashing.

    Raises:
        ValueError: If the file exists but is malformed (invalid TOML
            or a ``[[repo]]`` entry with no ``name``).
    """
    target = path if path is not None else _default_config_path()

    if not target.exists():
        return AutofixConfig()

    try:
        with target.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Failed to parse {target}: {exc}") from exc

    try:
        poll_interval = int(raw.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        poll_interval = DEFAULT_POLL_INTERVAL_SECONDS
    if poll_interval <= 0:
        poll_interval = DEFAULT_POLL_INTERVAL_SECONDS

    try:
        log_budget = int(raw.get("log_byte_budget", DEFAULT_LOG_BYTE_BUDGET))
    except (TypeError, ValueError):
        log_budget = DEFAULT_LOG_BYTE_BUDGET
    if log_budget <= 0:
        log_budget = DEFAULT_LOG_BYTE_BUDGET

    repos = _coerce_repos(raw.get("repo"))

    return AutofixConfig(
        poll_interval_seconds=poll_interval,
        log_byte_budget=log_budget,
        repos=repos,
    )


def default_config_path() -> Path:
    """Public shim around :func:`_default_config_path`."""
    return _default_config_path()
