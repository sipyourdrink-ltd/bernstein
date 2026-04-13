"""Runtime state helpers for Track B operational metadata.

This module centralizes lightweight runtime metadata used by the CLI,
server routes, replay tooling, and supervisor integration.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_SUPERVISOR_STATE_FILE = "supervisor_state.json"
_SESSION_METADATA_FILE = "metadata.json"
_CONFIG_STATE_FILE = "config_state.json"
_LOG_ROTATE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class SupervisorStateSnapshot:
    """Supervisor-managed server restart metadata."""

    started_at: float
    restart_count: int
    current_pid: int
    last_restart_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the snapshot for JSON persistence."""
        return asdict(self)


@dataclass(frozen=True)
class SessionReplayMetadata:
    """Stable metadata written beside replay logs for run identification."""

    run_id: str
    started_at: float
    git_sha: str
    git_branch: str
    config_hash: str
    seed_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the metadata for JSON persistence."""
        return asdict(self)


def rotate_log_file(log_path: Path, *, max_bytes: int = _LOG_ROTATE_BYTES) -> bool:
    """Rotate *log_path* to ``.1`` when it exceeds *max_bytes*.

    Args:
        log_path: Log file to rotate.
        max_bytes: Rotation threshold in bytes.

    Returns:
        True when rotation happened, False otherwise.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return False
    except OSError as exc:
        logger.debug("Cannot stat log %s for rotation: %s", log_path, exc)
        return False

    rotated = log_path.with_suffix(log_path.suffix + ".1")
    with contextlib.suppress(OSError):
        rotated.unlink()
    try:
        shutil.move(str(log_path), str(rotated))
        logger.info("Rotated log %s -> %s", log_path.name, rotated.name)
        return True
    except OSError as exc:
        logger.warning("Failed to rotate log %s: %s", log_path, exc)
        return False


def write_supervisor_state(workdir: Path, snapshot: SupervisorStateSnapshot) -> Path:
    """Persist the current supervisor snapshot under ``.sdd/runtime``."""
    runtime_dir = workdir / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / _SUPERVISOR_STATE_FILE
    path.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
    return path


def read_supervisor_state(sdd_dir: Path) -> SupervisorStateSnapshot | None:
    """Load the latest supervisor snapshot from disk."""
    path = sdd_dir / "runtime" / _SUPERVISOR_STATE_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return SupervisorStateSnapshot(
            started_at=float(raw.get("started_at", 0.0)),
            restart_count=int(raw.get("restart_count", 0)),
            current_pid=int(raw.get("current_pid", 0)),
            last_restart_at=(float(raw["last_restart_at"]) if raw.get("last_restart_at") is not None else None),
        )
    except (TypeError, ValueError):
        return None


def write_session_replay_metadata(sdd_dir: Path, metadata: SessionReplayMetadata) -> Path:
    """Persist replay metadata next to the run's ``replay.jsonl`` file."""
    run_dir = sdd_dir / "runs" / metadata.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / _SESSION_METADATA_FILE
    path.write_text(json.dumps(metadata.to_dict(), indent=2), encoding="utf-8")
    return path


def read_session_replay_metadata(run_dir: Path) -> SessionReplayMetadata | None:
    """Load replay metadata from *run_dir* when available."""
    path = run_dir / _SESSION_METADATA_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return SessionReplayMetadata(
            run_id=str(raw.get("run_id", run_dir.name)),
            started_at=float(raw.get("started_at", 0.0)),
            git_sha=str(raw.get("git_sha", "")),
            git_branch=str(raw.get("git_branch", "")),
            config_hash=str(raw.get("config_hash", "")),
            seed_path=str(raw["seed_path"]) if raw.get("seed_path") else None,
        )
    except (TypeError, ValueError):
        return None


def write_config_state(
    sdd_dir: Path,
    *,
    config_hash: str,
    seed_path: str | None,
    reloaded_at: float,
    last_diff: dict[str, Any] | None = None,
) -> Path:
    """Persist the latest loaded ``bernstein.yaml`` metadata."""
    runtime_dir = sdd_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / _CONFIG_STATE_FILE
    payload = {
        "config_hash": config_hash,
        "seed_path": seed_path,
        "reloaded_at": reloaded_at,
        "last_diff": last_diff,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_config_state(sdd_dir: Path) -> dict[str, Any] | None:
    """Load the latest config-reload metadata from disk."""
    path = sdd_dir / "runtime" / _CONFIG_STATE_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def current_git_branch(workdir: Path) -> str:
    """Return the current git branch for *workdir* when available."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def current_git_sha(workdir: Path) -> str:
    """Return the current git SHA for *workdir* when available."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def hash_file(path: Path | None) -> str:
    """Return a SHA-256 hex digest for *path*, or empty string when missing."""
    if path is None or not path.exists():
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def directory_size_bytes(path: Path) -> int:
    """Return the total byte size of *path* recursively."""
    if not path.exists():
        return 0
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        return total
    return total


def memory_usage_mb() -> float:
    """Return current process memory usage in MB.

    ``ru_maxrss`` is kilobytes on Linux and bytes on macOS, so normalize both.
    Returns 0.0 on Windows where ``resource`` is unavailable.
    """
    try:
        import resource
    except ModuleNotFoundError:
        return 0.0
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return round(rss / (1024 * 1024), 2)
    return round(rss / 1024, 2)


def read_last_archive_record(archive_path: Path) -> dict[str, Any] | None:
    """Return the most recent archive JSON object from *archive_path*."""
    if not archive_path.exists():
        return None
    try:
        lines = [line for line in archive_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return None
    for raw in reversed(lines):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
