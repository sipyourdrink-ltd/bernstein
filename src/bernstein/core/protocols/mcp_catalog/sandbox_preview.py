"""Sandboxed dry-run install previews.

The catalog never trusts a manifest: every install runs the manifest's
``install_command`` inside an isolated working directory first, the
exit code, stdout, stderr and a diff of files the command touched are
captured, and only on a successful preview AND user confirmation does
Bernstein write to the host MCP config (acceptance criterion).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.protocols.mcp_catalog.manifest import CatalogEntry


@dataclass(frozen=True)
class FileDiff:
    """Diff entry summarising a single file change inside the sandbox.

    Attributes:
        path: Path relative to the sandbox root.
        change_type: One of ``"added"``, ``"modified"``, ``"removed"``.
        size_bytes: Final file size in bytes (``0`` for removed files).
    """

    path: str
    change_type: str
    size_bytes: int


@dataclass(frozen=True)
class InstallPreview:
    """Result of running ``install_command`` inside a sandbox tempdir.

    Attributes:
        exit_code: Process exit code. ``0`` indicates success.
        stdout: Captured stdout bytes.
        stderr: Captured stderr bytes.
        duration_seconds: Wall-clock duration.
        diff: File-tree diff between sandbox state before and after.
        sandbox_root: Tempdir the command ran in (already cleaned up).
        timed_out: Whether the run was killed by the timeout watchdog.
    """

    exit_code: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    diff: tuple[FileDiff, ...]
    sandbox_root: str
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        """True when exit code is zero and the run did not time out."""
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class SandboxRunner:
    """Pluggable sandbox executor.

    The default implementation uses :func:`subprocess.run` against a
    fresh tempdir. Tests can swap in a fake by passing a callable to
    :func:`run_install_preview`. We keep the surface minimal — the
    catalog only needs ``run`` semantics (capture stdout/stderr/exit)
    plus a directory snapshot to compute diffs.

    Attributes:
        timeout_seconds: Wall-clock cap for ``install_command``.
        env: Extra environment variables. ``None`` inherits parent env.
        executable_overrides: Optional map of ``argv[0]`` -> absolute
            path used when the host PATH does not contain the binary
            (useful for tests).
    """

    timeout_seconds: int = 120
    env: dict[str, str] | None = None
    executable_overrides: dict[str, str] = field(default_factory=dict)


def _snapshot_tree(root: Path) -> dict[str, tuple[int, str]]:
    """Snapshot the current file tree under ``root``.

    Returns a mapping of relative path -> ``(size, sha256-of-bytes)``.
    Symlinks are recorded by their target string instead of their
    contents to avoid following links out of the sandbox.
    """
    snapshot: dict[str, tuple[int, str]] = {}
    if not root.exists():
        return snapshot
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            absolute = Path(dirpath) / name
            try:
                rel = absolute.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                if absolute.is_symlink():
                    target = os.readlink(absolute)
                    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
                    snapshot[rel] = (len(target), digest)
                    continue
                stat = absolute.stat()
                with absolute.open("rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
                snapshot[rel] = (stat.st_size, digest)
            except OSError:
                continue
    return snapshot


def _compute_diff(
    before: dict[str, tuple[int, str]],
    after: dict[str, tuple[int, str]],
) -> tuple[FileDiff, ...]:
    """Compute a sorted file-level diff between two snapshots."""
    diffs: list[FileDiff] = []
    after_keys = set(after.keys())
    before_keys = set(before.keys())

    for path in sorted(after_keys - before_keys):
        size, _ = after[path]
        diffs.append(FileDiff(path=path, change_type="added", size_bytes=size))

    for path in sorted(before_keys - after_keys):
        diffs.append(FileDiff(path=path, change_type="removed", size_bytes=0))

    for path in sorted(after_keys & before_keys):
        if before[path] != after[path]:
            size, _ = after[path]
            diffs.append(
                FileDiff(path=path, change_type="modified", size_bytes=size)
            )

    return tuple(diffs)


def run_install_preview(
    entry: CatalogEntry,
    *,
    runner: SandboxRunner | None = None,
    sandbox_root: Path | None = None,
) -> InstallPreview:
    """Run ``entry.install_command`` inside a fresh tempdir.

    The host filesystem outside the tempdir is never touched: subprocess
    is launched with ``cwd`` set to the tempdir and the parent caller
    must NOT pass an alternative cwd. The tempdir is removed before
    returning so the diff is the only evidence of what the command did.

    Args:
        entry: Manifest entry to preview.
        runner: Optional :class:`SandboxRunner` controlling timeout/env.
        sandbox_root: Optional explicit tempdir (testing). When ``None``
            a random tempdir under :func:`tempfile.gettempdir` is used.

    Returns:
        An :class:`InstallPreview` describing the run.
    """
    runner = runner or SandboxRunner()
    cleanup = sandbox_root is None
    if sandbox_root is None:
        sandbox_root = Path(tempfile.mkdtemp(prefix="bernstein-mcp-catalog-"))
    sandbox_root.mkdir(parents=True, exist_ok=True)

    argv = list(entry.install_command)
    if argv and argv[0] in runner.executable_overrides:
        argv[0] = runner.executable_overrides[argv[0]]

    env: dict[str, str] = dict(os.environ)
    if runner.env is not None:
        env.update(runner.env)
    # Make the sandbox feel sandbox-y: never write to host caches.
    env.setdefault("HOME", str(sandbox_root))
    env.setdefault("XDG_CACHE_HOME", str(sandbox_root / ".cache"))

    before = _snapshot_tree(sandbox_root)

    start = time.monotonic()
    timed_out = False
    try:
        result = subprocess.run(
            argv,
            cwd=sandbox_root,
            env=env,
            capture_output=True,
            timeout=runner.timeout_seconds,
            check=False,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
    except FileNotFoundError as exc:
        # Treat a missing executable as a failed preview rather than
        # crashing the CLI: the user sees the captured stderr.
        exit_code = 127
        stdout = b""
        stderr = f"{argv[0]}: command not found ({exc})".encode()
    duration = time.monotonic() - start
    after = _snapshot_tree(sandbox_root)
    diff = _compute_diff(before, after)
    sandbox_str = str(sandbox_root)

    if cleanup:
        shutil.rmtree(sandbox_root, ignore_errors=True)

    return InstallPreview(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
        diff=diff,
        sandbox_root=sandbox_str,
        timed_out=timed_out,
    )


__all__ = [
    "FileDiff",
    "InstallPreview",
    "SandboxRunner",
    "run_install_preview",
]
