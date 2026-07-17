"""WEB-012: Dashboard task detail view with live log streaming via SSE.

GET /dashboard/tasks/{task_id} - task detail JSON
GET /dashboard/tasks/{task_id}/logs/stream - SSE log stream
GET /dashboard/tasks/{task_id}/diff - task diff (unified + structured)
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from bernstein.core.server import TaskResponse, TaskStore, read_log_tail, task_to_response

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

router = APIRouter()

_POLL_INTERVAL: float = 1.0
_MAX_IDLE_TICKS: int = 120  # Stop after 2 minutes of no new content


class TaskDetailResponse(BaseModel):
    """Detailed task view including log tail and progress."""

    task: TaskResponse
    log_tail: str
    log_size: int
    progress_entries: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    agent_status: str = ""
    #: Agent-posted, journal-anchored artifacts with per-version verification
    #: state (#2553). A version whose stored blob fails its hash check is marked
    #: unverified with no content -- the surface renders it tampered, not as
    #: content.
    artifacts: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    #: The chain-computed progress vector (#2553): a projection of journaled
    #: work, never self-reported. ``None`` until the task has journaled work.
    progress: dict[str, Any] | None = None


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _get_runtime_dir(request: Request) -> Path:
    return request.app.state.runtime_dir  # type: ignore[no-any-return]


def _get_workdir(request: Request) -> Path:
    return request.app.state.workdir  # type: ignore[no-any-return]


def _get_agent_log_path(runtime_dir: Path, session_id: str) -> Path | None:
    """Resolve the log file path for an agent session."""
    log_dir = runtime_dir / "logs"
    if not log_dir.exists():
        return None
    # Try exact match
    log_path = log_dir / f"{session_id}.log"
    if log_path.exists():
        return log_path
    # Try glob match
    matches = list(log_dir.glob(f"{session_id}*.log"))
    if matches:
        return matches[0]
    return None


@router.get("/dashboard/tasks/{task_id}", responses={404: {"description": "Task not found"}})
def task_detail(request: Request, task_id: str) -> TaskDetailResponse:
    """Return detailed task view including log tail and progress.

    Args:
        task_id: Task identifier.
    """
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    runtime_dir = _get_runtime_dir(request)
    log_tail = ""
    log_size = 0
    agent_status = ""

    if task.assigned_agent:
        log_path = _get_agent_log_path(runtime_dir, task.assigned_agent)
        if log_path is not None:
            log_tail = read_log_tail(log_path)
            log_size = len(log_tail.encode("utf-8"))
        agent_status = "assigned"

    # Build progress entries from task.progress_log (list[dict[str, Any]])
    progress_entries: list[dict[str, Any]] = [
        {
            "message": str(entry.get("message", "")),
            "percent": int(entry.get("percent", 0)),
            "timestamp": float(entry.get("timestamp", 0)),
        }
        for entry in task.progress_log
    ]

    artifacts, progress = _artifacts_and_progress(request, task_id)

    return TaskDetailResponse(
        task=task_to_response(task),
        log_tail=log_tail,
        log_size=log_size,
        progress_entries=progress_entries,
        agent_status=agent_status,
        artifacts=artifacts,
        progress=progress,
    )


def _get_sdd_dir(request: Request) -> Path | None:
    return getattr(request.app.state, "sdd_dir", None)


def _artifacts_and_progress(request: Request, task_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Project a task's agent-posted artifacts and chain-computed progress (#2553).

    Both surfaces are read-only projections of on-disk chain records. A missing
    ``.sdd`` directory or an empty journal yields ``([], None)``.
    """
    sdd_dir = _get_sdd_dir(request)
    if sdd_dir is None:
        return [], None
    from bernstein.core.routes.task_artifacts import build_artifact_views, build_progress_response

    artifacts = [view.model_dump() for view in build_artifact_views(sdd_dir, task_id)]
    progress: dict[str, Any] | None = None
    if artifacts or (sdd_dir / "runs").is_dir():
        progress = build_progress_response(sdd_dir, task_id).model_dump()
    return artifacts, progress


def _try_read_session_log(
    session_id: str,
    runtime_dir: Any,
    read_fn: Any,
    last_size: int,
) -> tuple[str, int] | None:
    """Try to read new log content for a session, returning (content, new_size) or None."""
    if not session_id:
        return None
    log_path = _get_agent_log_path(runtime_dir, session_id)
    if log_path is None or not log_path.exists():
        return None
    return read_fn(log_path, last_size)


@router.get("/dashboard/tasks/{task_id}/logs/stream", responses={404: {"description": "Task not found"}})
async def task_log_stream(request: Request, task_id: str) -> StreamingResponse:
    """Stream agent logs for a task via Server-Sent Events.

    The stream sends new log content as ``log`` events and closes
    after the task completes or ``_MAX_IDLE_TICKS`` seconds of no new data.
    """
    store = _get_store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    runtime_dir = _get_runtime_dir(request)

    def _read_new_log_content(log_path: Path, last_size: int) -> tuple[str, int] | None:
        """Read new bytes appended to a log file since *last_size*.

        Returns (new_content, new_size) or None if nothing new.
        """
        current_size = log_path.stat().st_size
        if current_size <= last_size:
            return None
        with log_path.open(encoding="utf-8", errors="replace") as f:
            f.seek(last_size)
            return f.read(), current_size

    async def _stream_logs() -> AsyncGenerator[str, None]:
        last_size = 0
        idle_ticks = 0
        session_id = task.assigned_agent or ""

        while idle_ticks < _MAX_IDLE_TICKS:
            if await request.is_disconnected():
                return

            current_task = store.get_task(task_id)
            if current_task is not None and current_task.status.value in ("done", "failed", "cancelled"):
                yield f'event: complete\ndata: {{"status": "{current_task.status.value}"}}\n\n'
                break

            new_data = _try_read_session_log(session_id, runtime_dir, _read_new_log_content, last_size)
            if new_data is not None:
                new_content, last_size = new_data
                idle_ticks = 0
                for line in new_content.splitlines():
                    yield f"event: log\ndata: {line}\n\n"
                continue

            idle_ticks += 1
            yield f'event: ping\ndata: {{"ts": {time.time()}}}\n\n'
            await asyncio.sleep(_POLL_INTERVAL)

        yield "event: close\ndata: {}\n\n"

    return StreamingResponse(
        _stream_logs(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Diff endpoint (WEB-012/diff)
# ---------------------------------------------------------------------------


_DIFF_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB safety cap
_DIFF_TIMEOUT_S = 15


class DiffHunk(BaseModel):
    """A single hunk in a file diff."""

    header: str
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: list[str]


class DiffFile(BaseModel):
    """Per-file diff entry."""

    path: str
    old_path: str | None = None
    status: str = "modified"  # added | deleted | renamed | modified | binary
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    language: str | None = None
    hunks: list[DiffHunk] = Field(default_factory=list)


class TaskDiffResponse(BaseModel):
    """Diff payload for a task's working branch vs the base ref."""

    task_id: str
    branch: str | None
    base_ref: str
    head_ref: str | None
    additions: int
    deletions: int
    files: list[DiffFile]
    unified: str
    truncated: bool = False
    generated_at: float
    note: str | None = None


def _run_git(args: list[str], cwd: Path, *, timeout: int = _DIFF_TIMEOUT_S) -> tuple[int, str, str]:
    """Thin wrapper over subprocess.run that never raises."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "", "git not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} timed out after {timeout}s"
    return proc.returncode, proc.stdout, proc.stderr


_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "ts",
    ".tsx": "tsx",
    ".js": "js",
    ".jsx": "jsx",
    ".mjs": "js",
    ".cjs": "js",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".rst": "rst",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".sql": "sql",
    ".proto": "proto",
    ".xml": "xml",
}


def _language_for(path: str) -> str | None:
    if not path:
        return None
    if path.endswith("Dockerfile") or "/Dockerfile" in path:
        return "dockerfile"
    if path.endswith("Makefile") or "/Makefile" in path:
        return "makefile"
    for ext, lang in _LANG_BY_EXT.items():
        if path.endswith(ext):
            return lang
    return None


_HUNK_RE = re.compile(
    r"^@@ -(?P<os>\d+)(?:,(?P<ol>\d+))? \+(?P<ns>\d+)(?:,(?P<nl>\d+))? @@",
)


def _parse_unified_diff(text: str) -> list[DiffFile]:
    """Parse a unified-diff blob into structured DiffFile records.

    The parser is forgiving: anything it does not understand becomes part of
    the previous hunk's lines (or is dropped if there is no current hunk).
    """
    files: list[DiffFile] = []
    cur: DiffFile | None = None
    cur_hunk: DiffHunk | None = None

    def _finalise_hunk() -> None:
        nonlocal cur_hunk
        if cur is not None and cur_hunk is not None:
            cur.hunks.append(cur_hunk)
        cur_hunk = None

    def _finalise_file() -> None:
        nonlocal cur
        _finalise_hunk()
        if cur is not None:
            files.append(cur)
        cur = None

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            _finalise_file()
            # diff --git a/<path> b/<path>
            parts = line.split(" ")
            path = ""
            if len(parts) >= 4:
                path = parts[-1].removeprefix("b/")
            cur = DiffFile(path=path, language=_language_for(path))
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        if line.startswith("rename from "):
            cur.status = "renamed"
            cur.old_path = line[len("rename from ") :]
        elif line.startswith("rename to "):
            cur.path = line[len("rename to ") :]
            cur.language = _language_for(cur.path)
        elif line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith(("Binary files ", "GIT binary patch")):
            cur.binary = True
            cur.status = "binary"
        elif line.startswith("--- "):
            # old path; ignore if /dev/null (already covered by status)
            pass
        elif line.startswith("+++ "):
            target = line[4:].removeprefix("b/")
            if target and target != "/dev/null":
                cur.path = target
                cur.language = _language_for(cur.path)
        elif line.startswith("@@"):
            _finalise_hunk()
            m = _HUNK_RE.match(line)
            if m is not None:
                cur_hunk = DiffHunk(
                    header=line,
                    old_start=int(m.group("os")),
                    old_lines=int(m.group("ol") or "1"),
                    new_start=int(m.group("ns")),
                    new_lines=int(m.group("nl") or "1"),
                    lines=[],
                )
            else:
                cur_hunk = DiffHunk(
                    header=line,
                    old_start=0,
                    old_lines=0,
                    new_start=0,
                    new_lines=0,
                    lines=[],
                )
        else:
            if cur_hunk is not None:
                cur_hunk.lines.append(line)
                if line.startswith("+") and not line.startswith("+++"):
                    cur.additions += 1
                elif line.startswith("-") and not line.startswith("---"):
                    cur.deletions += 1
        i += 1

    _finalise_file()
    return files


def _resolve_base_ref(workdir: Path) -> str:
    """Return the best available base ref (main, master, or HEAD)."""
    for ref in ("main", "master"):
        rc, _, _ = _run_git(["rev-parse", "--verify", f"refs/heads/{ref}"], workdir, timeout=5)
        if rc == 0:
            return ref
        rc2, _, _ = _run_git(["rev-parse", "--verify", ref], workdir, timeout=5)
        if rc2 == 0:
            return ref
    return "HEAD"


def _resolve_branch_for_task(workdir: Path, assigned_agent: str | None) -> str | None:
    """Pick the working branch for *assigned_agent*, if one exists."""
    if not assigned_agent:
        return None
    candidate = f"agent/{assigned_agent}"
    rc, _, _ = _run_git(["rev-parse", "--verify", candidate], workdir, timeout=5)
    if rc == 0:
        return candidate
    # Fall back to a refs/heads/agent/* glob match - the session prefix may
    # not be a complete branch name.
    rc, out, _ = _run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/agent/"],
        workdir,
        timeout=5,
    )
    if rc == 0:
        prefix = f"agent/{assigned_agent}"
        for branch in out.splitlines():
            branch = branch.strip()
            if branch == prefix or branch.startswith((prefix + "-", prefix)):
                return branch
    return None


@router.get(
    "/dashboard/tasks/{task_id}/diff",
    responses={404: {"description": "Task not found"}},
)
async def task_diff(request: Request, task_id: str) -> TaskDiffResponse:
    """Return the diff for a task's working branch against the base ref.

    Strategy:
        1. Resolve the working branch from the task's ``assigned_agent`` --
           ``agent/<session-id>``. If no agent is assigned (or the branch
           does not exist yet), fall back to ``git diff HEAD`` so the user
           still sees uncommitted scratch work.
        2. Run ``git diff <base>...<branch>`` (three-dot, symmetric
           difference relative to the merge base) and parse the output into
           a structured per-file representation.
        3. Cap the unified diff at ``_DIFF_MAX_BYTES`` to keep payloads sane.

    The sync ``_run_git`` helper is reused (it is also called from other
    sync helpers in this module). To keep the event loop responsive under
    load (issue #1723) every blocking ``_run_git`` invocation is offloaded
    to the default executor via ``asyncio.to_thread``. The helper itself
    stays sync so non-route callers keep working.
    """
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    workdir = _get_workdir(request)
    base_ref = await asyncio.to_thread(_resolve_base_ref, workdir)
    branch = await asyncio.to_thread(_resolve_branch_for_task, workdir, task.assigned_agent)
    note: str | None = None

    if branch is not None:
        head_ref = branch
        rc, raw, err = await asyncio.to_thread(
            _run_git,
            ["diff", "--no-color", f"{base_ref}...{branch}"],
            workdir,
            timeout=_DIFF_TIMEOUT_S,
        )
    else:
        head_ref = None
        rc, raw, err = await asyncio.to_thread(
            _run_git,
            ["diff", "--no-color", "HEAD"],
            workdir,
            timeout=_DIFF_TIMEOUT_S,
        )
        if task.assigned_agent:
            note = "No agent worktree branch found; showing working-tree diff against HEAD."
        else:
            note = "Task is not yet assigned to an agent; showing the current working-tree diff against HEAD."

    if rc not in (0, 1):  # git diff returns 1 when there are differences
        # Non-zero, non-1 → real failure. Surface as empty diff with note.
        raw = ""
        note = (note + " " if note else "") + (err.strip() or "git diff failed")

    truncated = False
    encoded = raw.encode("utf-8", errors="replace")
    if len(encoded) > _DIFF_MAX_BYTES:
        truncated = True
        raw = encoded[:_DIFF_MAX_BYTES].decode("utf-8", errors="replace")

    files = _parse_unified_diff(raw)
    additions = sum(f.additions for f in files)
    deletions = sum(f.deletions for f in files)

    return TaskDiffResponse(
        task_id=task_id,
        branch=branch,
        base_ref=base_ref,
        head_ref=head_ref,
        additions=additions,
        deletions=deletions,
        files=files,
        unified=raw,
        truncated=truncated,
        generated_at=time.time(),
        note=note,
    )
