"""Agent execution trace storage, parsing, and replay utilities.

Each agent execution produces a structured trace stored in .sdd/traces/.
Traces capture decision points: files read, edits made, tests run, and outcome.
"""

from __future__ import annotations

import difflib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TraceStep:
    """A single decision point in an agent's execution.

    Attributes:
        type: Step category (orient, plan, edit, verify, spawn, complete, fail).
        timestamp: Unix timestamp when this step occurred.
        detail: Human-readable description of what happened.
        files: Files involved in this step (reads or writes).
        tokens: Approximate tokens consumed (if known).
        duration_ms: Duration in milliseconds (if known).
    """

    type: Literal["spawn", "orient", "plan", "edit", "verify", "complete", "fail"]
    timestamp: float
    detail: str = ""
    files: list[str] = field(default_factory=lambda: [])
    tokens: int = 0
    duration_ms: int = 0
    # Per-turn budget accounting (populated at turn boundaries)
    turn_number: int = 0
    allocated_budget: int = 0
    consumed_this_turn: int = 0
    remaining_budget: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TraceStep:
        return cls(
            type=d["type"],
            timestamp=d["timestamp"],
            detail=d.get("detail", ""),
            files=cast("list[str]", d.get("files", [])),
            tokens=cast("int", d.get("tokens", 0)),
            duration_ms=cast("int", d.get("duration_ms", 0)),
            turn_number=cast("int", d.get("turn_number", 0)),
            allocated_budget=cast("int", d.get("allocated_budget", 0)),
            consumed_this_turn=cast("int", d.get("consumed_this_turn", 0)),
            remaining_budget=cast("int", d.get("remaining_budget", 0)),
        )


@dataclass
class AgentTrace:
    """Full execution trace for one agent session.

    Attributes:
        trace_id: Unique ID for this trace.
        session_id: Agent session ID (matches log filename).
        task_ids: Task IDs handled by this agent.
        agent_role: Role of the agent (e.g. "backend", "qa").
        model: Model short name (e.g. "sonnet", "opus").
        effort: Effort level (e.g. "high", "max").
        spawn_ts: Unix timestamp when agent was spawned.
        end_ts: Unix timestamp when agent was reaped (None if still running).
        steps: Ordered list of decision points.
        outcome: Final outcome: "success", "failed", or "unknown".
        log_path: Path to the agent log file.
        task_snapshots: Serialized task dicts (for replay without server).
    """

    trace_id: str
    session_id: str
    task_ids: list[str]
    agent_role: str
    model: str
    effort: str
    spawn_ts: float
    end_ts: float | None = None
    steps: list[TraceStep] = field(default_factory=lambda: [])
    outcome: Literal["success", "failed", "unknown"] = "unknown"
    log_path: str = ""
    task_snapshots: list[dict[str, Any]] = field(default_factory=lambda: [])
    # Budget snapshot at turn boundaries
    total_allocated_budget: int = 0
    total_consumed: int = 0
    turn_count: int = 0
    # Settings snapshot captured at spawn time (T557)
    settings_snapshot: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def duration_s(self) -> float | None:
        if self.end_ts is None:
            return None
        return self.end_ts - self.spawn_ts

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentTrace:
        steps = [TraceStep.from_dict(s) for s in cast("list[dict[str, Any]]", d.get("steps", []))]
        return cls(
            trace_id=d["trace_id"],
            session_id=d["session_id"],
            task_ids=cast("list[str]", d.get("task_ids", [])),
            agent_role=d.get("agent_role", ""),
            model=d.get("model", ""),
            effort=d.get("effort", ""),
            spawn_ts=d.get("spawn_ts", 0.0),
            end_ts=d.get("end_ts"),
            steps=steps,
            outcome=d.get("outcome", "unknown"),
            log_path=d.get("log_path", ""),
            task_snapshots=cast("list[dict[str, Any]]", d.get("task_snapshots", [])),
            settings_snapshot=cast("dict[str, Any]", d.get("settings_snapshot", {})),
        )


@dataclass(frozen=True)
class ReplayTaskRequest:
    """Replay request built from a stored trace snapshot."""

    title: str
    description: str
    role: str
    priority: int
    scope: str
    complexity: str
    model: str
    effort: str
    original_result_summary: str

    def to_payload(self) -> dict[str, Any]:
        """Serialize the replay request into a task-create payload."""
        return {
            "title": self.title,
            "description": self.description,
            "role": self.role,
            "priority": self.priority,
            "scope": self.scope,
            "complexity": self.complexity,
            "model": self.model,
            "effort": self.effort,
        }


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------


# Regex patterns for log lines emitted by the Claude Code wrapper script.
# Format: [ToolName] input_truncated...
_TOOL_RE = re.compile(r"^\[(?P<tool>[A-Za-z]+)\]\s*(?P<args>.*)$")

# File-reading tools → orient steps
_ORIENT_TOOLS = {"Read", "Glob", "Grep"}
# File-writing tools → edit steps
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
# Execution tools → verify steps
_VERIFY_TOOLS = {"Bash", "Task", "WebFetch", "WebSearch"}


def parse_log_to_steps(log_path: Path) -> list[TraceStep]:
    """Parse a human-readable agent log into TraceStep list.

    The log is produced by the ClaudeCodeAdapter wrapper script which emits
    lines like ``[Read] /path/to/file`` for each tool use.

    Args:
        log_path: Path to the agent log file.

    Returns:
        List of TraceStep objects (empty if the log doesn't exist or is empty).
    """
    if not log_path.exists():
        return []

    steps: list[TraceStep] = []
    log_mtime = log_path.stat().st_mtime

    # We don't have per-line timestamps in the log, so we distribute them
    # evenly across the file's mtime range as a rough approximation.
    lines = log_path.read_text(errors="replace").splitlines()
    total = max(len(lines), 1)

    # Estimate start time: assume the file was written over ~60s before mtime
    estimated_start = log_mtime - min(60.0, log_mtime)
    time_span = max(log_mtime - estimated_start, 1.0)

    # Collect orient/edit/verify steps; collapse consecutive same-type runs.
    last_type: str | None = None
    current_files: list[str] = []
    current_ts: float = estimated_start

    def _flush(step_type: str, ts: float, files: list[str]) -> None:
        steps.append(
            TraceStep(
                type=step_type,  # type: ignore[arg-type]
                timestamp=ts,
                detail=f"{step_type.capitalize()}: {', '.join(files[:3])}{'...' if len(files) > 3 else ''}",
                files=files[:],
            )
        )

    for i, line in enumerate(lines):
        ts = estimated_start + (i / total) * time_span
        m = _TOOL_RE.match(line.strip())
        if m is None:
            # Plain text — could be assistant commentary (plan step)
            if line.strip() and last_type not in ("plan",):
                if last_type and current_files:
                    _flush(last_type, current_ts, current_files)
                    current_files = []
                last_type = "plan"
                current_ts = ts
                current_files = []
            continue

        tool = m.group("tool")
        args = m.group("args").strip()

        if tool in _ORIENT_TOOLS:
            step_type = "orient"
        elif tool in _EDIT_TOOLS:
            step_type = "edit"
        elif tool in _VERIFY_TOOLS:
            step_type = "verify"
        else:
            # Unknown tool — treat as plan/commentary
            step_type = "plan"

        if step_type != last_type:
            if last_type and (current_files or last_type == "plan"):
                _flush(last_type, current_ts, current_files)
                current_files = []
            last_type = step_type
            current_ts = ts

        # Extract filename from args (heuristic: first path-looking token)
        file_hint = _extract_file_hint(args)
        if file_hint:
            current_files.append(file_hint)

    # Flush remaining
    if last_type and (current_files or last_type == "plan"):
        _flush(last_type, current_ts, current_files)

    return steps


def parse_agent_log(log_path: Path) -> list[TraceStep]:
    """Parse an agent log file into a list of TraceStep objects.

    Alias for :func:`parse_log_to_steps` that matches the public API name
    specified in the trace enhancement spec.

    Args:
        log_path: Path to the agent log file produced by the wrapper script.

    Returns:
        List of TraceStep objects (empty if the log doesn't exist or is empty).
    """
    return parse_log_to_steps(log_path)


def _extract_file_hint(args: str) -> str:
    """Try to extract a file path from a tool argument string.

    Args:
        args: Truncated input string from the wrapper log.

    Returns:
        Best-guess file path, or the raw args string if no path detected.
    """
    # JSON object: look for "file_path", "path", or "pattern" key
    if args.startswith("{"):
        try:
            parsed = json.loads(args)
            for key in ("file_path", "path", "pattern", "command"):
                if key in parsed and isinstance(parsed[key], str):
                    return parsed[key]
        except json.JSONDecodeError:
            pass

    # Plain path: starts with / or ./ or src/
    stripped = args.strip().strip("\"'")
    if stripped.startswith(("/", "./", "src/", "tests/", "templates/")):
        return stripped.split()[0]

    # Short non-space string — could be a filename
    if " " not in stripped and len(stripped) < 120:
        return stripped

    return args[:80] if args else ""


# ---------------------------------------------------------------------------
# Trace store
# ---------------------------------------------------------------------------


class TraceStore:
    """Read and write agent traces to .sdd/traces/.

    Traces are stored as JSONL files: one file per task ID, one JSON line
    per trace (so multiple retries of the same task accumulate in one file).

    Args:
        traces_dir: Path to the traces directory (usually .sdd/traces/).
    """

    def __init__(self, traces_dir: Path) -> None:
        self._dir = traces_dir

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for_task(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.jsonl"

    def _path_for_trace(self, trace_id: str) -> Path:
        return self._dir / f"trace-{trace_id}.json"

    def write(self, trace: AgentTrace) -> None:
        """Persist a trace to disk.

        Writes to two locations:
        - ``{traces_dir}/{task_id}.jsonl`` (one line per trace, appended)
        - ``{traces_dir}/trace-{trace_id}.json`` (single-trace file for direct lookup)

        Args:
            trace: The trace to persist.
        """
        self._ensure_dir()
        data = json.dumps(trace.to_dict())

        # Write per-trace file (overwrites on update)
        self._path_for_trace(trace.trace_id).write_text(data + "\n")

        # Append to per-task JSONL
        for task_id in trace.task_ids:
            task_path = self._path_for_task(task_id)
            with task_path.open("a") as fh:
                fh.write(data + "\n")

    def read_by_task(self, task_id: str) -> list[AgentTrace]:
        """Return all traces for a given task ID (most recent last).

        Args:
            task_id: Task ID to look up.

        Returns:
            List of AgentTrace objects, or empty list if none found.
        """
        path = self._path_for_task(task_id)
        if not path.exists():
            return []
        traces: list[AgentTrace] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                traces.append(AgentTrace.from_dict(cast("dict[str, Any]", json.loads(line))))
            except (json.JSONDecodeError, KeyError):
                continue
        return traces

    def read_by_trace_id(self, trace_id: str) -> AgentTrace | None:
        """Return a specific trace by trace_id.

        Searches the per-trace file first (O(1)), then falls back to
        scanning all JSONL files.

        Args:
            trace_id: Trace ID to look up.

        Returns:
            AgentTrace, or None if not found.
        """
        direct = self._path_for_trace(trace_id)
        if direct.exists():
            try:
                return AgentTrace.from_dict(json.loads(direct.read_text().strip()))
            except (json.JSONDecodeError, KeyError):
                pass

        # Fallback: scan all task JSONL files
        for jsonl in self._dir.glob("*.jsonl"):
            for line in jsonl.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("trace_id") == trace_id:
                        return AgentTrace.from_dict(d)
                except (json.JSONDecodeError, KeyError):
                    continue
        return None

    def list_traces(self, limit: int = 50) -> list[AgentTrace]:
        """List recent traces, sorted by spawn time descending.

        Args:
            limit: Maximum number of traces to return.

        Returns:
            List of AgentTrace objects.
        """
        traces: list[AgentTrace] = []
        seen: set[str] = set()

        if not self._dir.exists():
            return []

        # Prefer per-trace JSON files for O(1) reads
        trace_files = sorted(
            self._dir.glob("trace-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for tf in trace_files[:limit]:
            try:
                t = AgentTrace.from_dict(json.loads(tf.read_text().strip()))
                if t.trace_id not in seen:
                    seen.add(t.trace_id)
                    traces.append(t)
            except (json.JSONDecodeError, KeyError):
                continue

        traces.sort(key=lambda t: t.spawn_ts, reverse=True)
        return traces[:limit]

    def latest_for_task(self, task_id: str) -> AgentTrace | None:
        """Return the newest trace recorded for a given task ID."""
        traces = self.read_by_task(task_id)
        if not traces:
            return None
        return max(traces, key=lambda trace: trace.spawn_ts)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def new_trace(
    session_id: str,
    task_ids: list[str],
    role: str,
    model: str,
    effort: str,
    log_path: str = "",
    task_snapshots: list[dict[str, Any]] | None = None,
) -> AgentTrace:
    """Create a new AgentTrace for a freshly spawned agent.

    Args:
        session_id: Agent session ID.
        task_ids: IDs of tasks assigned to this agent.
        role: Agent role name.
        model: Model short name.
        effort: Effort level.
        log_path: Path to the agent log file.
        task_snapshots: Optional serialized task dicts for replay.

    Returns:
        AgentTrace with a spawn step already appended.
    """
    trace = AgentTrace(
        trace_id=uuid.uuid4().hex[:16],
        session_id=session_id,
        task_ids=task_ids,
        agent_role=role,
        model=model,
        effort=effort,
        spawn_ts=time.time(),
        log_path=log_path,
        task_snapshots=task_snapshots or [],
    )
    trace.steps.append(
        TraceStep(
            type="spawn",
            timestamp=trace.spawn_ts,
            detail=f"Spawned {role} agent ({model}/{effort}) for tasks: {', '.join(task_ids)}",
        )
    )
    return trace


def finalize_trace(
    trace: AgentTrace,
    outcome: Literal["success", "failed", "unknown"],
    log_path: Path | None = None,
) -> AgentTrace:
    """Finalize a trace after the agent exits.

    Parses the agent log (if available) to extract decision steps, then
    appends a complete/fail outcome step.

    Args:
        trace: The trace to finalize (mutated in place).
        outcome: Final outcome of the task.
        log_path: Path to the agent log file for parsing.

    Returns:
        The mutated trace (same object).
    """
    trace.end_ts = time.time()
    trace.outcome = outcome

    # Parse log to extract intermediate steps
    effective_log = Path(trace.log_path) if trace.log_path else log_path
    if effective_log and effective_log.exists():
        parsed = parse_log_to_steps(effective_log)
        # Insert parsed steps between the spawn step and the outcome step
        trace.steps[1:1] = parsed

    trace.steps.append(
        TraceStep(
            type="complete" if outcome == "success" else "fail",
            timestamp=trace.end_ts,
            detail=f"Agent exited with outcome: {outcome}",
            duration_ms=int((trace.end_ts - trace.spawn_ts) * 1000),
        )
    )
    return trace


def build_replay_task_request(
    trace: AgentTrace,
    *,
    task_id: str | None = None,
    override_model: str | None = None,
    extra_context: str | None = None,
) -> ReplayTaskRequest:
    """Build a replay task payload from the latest trace snapshot."""
    snapshot: dict[str, Any] | None = None
    if task_id is not None:
        snapshot = next((item for item in trace.task_snapshots if item.get("id") == task_id), None)
    if snapshot is None and trace.task_snapshots:
        snapshot = trace.task_snapshots[0]
    if snapshot is None:
        raise ValueError("Trace does not contain any task snapshots to replay")

    original_description = str(snapshot.get("description", ""))
    description = original_description
    if extra_context:
        description = f"{description.rstrip()}\n\nReplay hint:\n{extra_context}"

    return ReplayTaskRequest(
        title=f"[replay] {snapshot.get('title', trace.task_ids[0] if trace.task_ids else trace.trace_id)}",
        description=description,
        role=str(snapshot.get("role", trace.agent_role)),
        priority=int(snapshot.get("priority", 2)),
        scope=str(snapshot.get("scope", "medium")),
        complexity=str(snapshot.get("complexity", "medium")),
        model=override_model or str(snapshot.get("model", trace.model)),
        effort=str(snapshot.get("effort", trace.effort)),
        original_result_summary=str(snapshot.get("result_summary", "")),
    )


def render_replay_diff(original: str, replayed: str) -> str:
    """Render a unified diff between the original and replayed task outcomes."""
    diff = difflib.unified_diff(
        original.splitlines(),
        replayed.splitlines(),
        fromfile="original",
        tofile="replay",
        lineterm="",
    )
    return "\n".join(diff)


def record_turn_budget(
    trace: AgentTrace,
    turn_number: int,
    allocated: int,
    consumed: int,
    remaining: int,
) -> TraceStep:
    """Create a TraceStep capturing per-turn budget accounting.

    Updates ``trace.turn_count`` and ``trace.total_consumed`` in-place
    so downstream analysis can evaluate efficiency vs budget.

    Args:
        trace: The agent trace to annotate.
        turn_number: 1-based turn/iteration number.
        allocated: Token budget allocated for this turn.
        consumed: Tokens actually consumed this turn.
        remaining: Remaining tokens after this turn.

    Returns:
        The created TraceStep (caller should append to trace.steps).
    """
    step = TraceStep(
        type="orient",  # reuse orient as a budget boundary marker
        timestamp=time.time(),
        detail=f"turn {turn_number}: budget {allocated}, consumed {consumed}, remaining {remaining}",
        turn_number=turn_number,
        allocated_budget=allocated,
        consumed_this_turn=consumed,
        remaining_budget=remaining,
    )
    trace.turn_count = max(trace.turn_count, turn_number)
    trace.total_consumed += consumed
    trace.total_allocated_budget = max(trace.total_allocated_budget, allocated)
    return step


# ---------------------------------------------------------------------------
# Fuzzy patch match confidence scoring (T566)
# ---------------------------------------------------------------------------


@dataclass
class PatchMatchResult:
    """Result of a fuzzy patch match attempt.

    Attributes:
        file_path: Path to the file being patched.
        confidence: Match confidence in [0.0, 1.0].
        matched: Whether the patch was applied successfully.
        before_snippet: First 200 chars of the original content.
        after_snippet: First 200 chars of the patched content.
        diff_lines: Number of lines changed.
        mismatch_reason: Human-readable reason if confidence < 1.0.
    """

    file_path: str
    confidence: float
    matched: bool
    before_snippet: str = ""
    after_snippet: str = ""
    diff_lines: int = 0
    mismatch_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "file_path": self.file_path,
            "confidence": self.confidence,
            "matched": self.matched,
            "before_snippet": self.before_snippet,
            "after_snippet": self.after_snippet,
            "diff_lines": self.diff_lines,
            "mismatch_reason": self.mismatch_reason,
        }


def score_patch_match(
    original: str,
    patched: str,
    file_path: str = "",
    *,
    context_lines: int = 3,
) -> PatchMatchResult:
    """Score how confidently a patch was applied to a file (T566).

    Uses difflib sequence matching to compute a similarity ratio between
    the original and patched content.  A ratio of 1.0 means no change
    (identity), while lower values indicate more aggressive edits.

    Args:
        original: Original file content before the patch.
        patched: File content after the patch was applied.
        file_path: Path label for the result.
        context_lines: Lines of context to include in snippets.

    Returns:
        :class:`PatchMatchResult` with confidence score and diff metadata.
    """
    if original == patched:
        return PatchMatchResult(
            file_path=file_path,
            confidence=1.0,
            matched=True,
            before_snippet=original[:200],
            after_snippet=patched[:200],
            diff_lines=0,
        )

    orig_lines = original.splitlines(keepends=True)
    patch_lines = patched.splitlines(keepends=True)

    matcher = difflib.SequenceMatcher(None, orig_lines, patch_lines, autojunk=False)
    ratio = matcher.ratio()

    # Count changed lines
    diff_lines = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")

    mismatch_reason = ""
    if ratio < 0.5:
        mismatch_reason = f"Low similarity ({ratio:.2f}) — patch may have applied to wrong location"
    elif ratio < 0.8:
        mismatch_reason = f"Moderate similarity ({ratio:.2f}) — verify patch applied correctly"

    return PatchMatchResult(
        file_path=file_path,
        confidence=ratio,
        matched=ratio >= 0.5,
        before_snippet=original[:200],
        after_snippet=patched[:200],
        diff_lines=diff_lines,
        mismatch_reason=mismatch_reason,
    )


# ---------------------------------------------------------------------------
# Structured file-edit conflict preview (T560)
# ---------------------------------------------------------------------------


@dataclass
class FileEditConflict:
    """Structured preview of a file-edit conflict between two agents.

    Attributes:
        file_path: Path to the conflicting file.
        session_a: Session ID of the first agent.
        session_b: Session ID of the second agent.
        snippet_a: Relevant snippet from agent A's edit.
        snippet_b: Relevant snippet from agent B's edit.
        conflict_lines: Line numbers where the conflict occurs.
        resolution_hint: Suggested resolution strategy.
    """

    file_path: str
    session_a: str
    session_b: str
    snippet_a: str = ""
    snippet_b: str = ""
    conflict_lines: list[int] = field(default_factory=list[int])
    resolution_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "file_path": self.file_path,
            "session_a": self.session_a,
            "session_b": self.session_b,
            "snippet_a": self.snippet_a,
            "snippet_b": self.snippet_b,
            "conflict_lines": self.conflict_lines,
            "resolution_hint": self.resolution_hint,
        }


def preview_edit_conflict(
    file_path: str,
    content_a: str,
    content_b: str,
    session_a: str = "",
    session_b: str = "",
) -> FileEditConflict:
    """Build a structured conflict preview for two competing edits (T560).

    Computes a unified diff between *content_a* and *content_b* and
    extracts the conflicting line ranges and representative snippets.

    Args:
        file_path: Path label for the conflict.
        content_a: File content from agent A.
        content_b: File content from agent B.
        session_a: Session ID of agent A.
        session_b: Session ID of agent B.

    Returns:
        :class:`FileEditConflict` with diff metadata and resolution hint.
    """
    lines_a = content_a.splitlines(keepends=True)
    lines_b = content_b.splitlines(keepends=True)

    diff = list(difflib.unified_diff(lines_a, lines_b, fromfile=f"{file_path} (A)", tofile=f"{file_path} (B)", n=2))

    conflict_lines: list[int] = []
    for line in diff:
        if line.startswith("@@"):
            # Extract line numbers from @@ -a,b +c,d @@ header
            m = re.search(r"\+(\d+)", line)
            if m:
                conflict_lines.append(int(m.group(1)))

    snippet_a = content_a[:300]
    snippet_b = content_b[:300]

    hint = "manual merge required"
    if not diff:
        hint = "no conflict — contents are identical"
    elif len(conflict_lines) == 1:
        hint = f"single-region conflict at line {conflict_lines[0]} — prefer agent with later timestamp"

    return FileEditConflict(
        file_path=file_path,
        session_a=session_a,
        session_b=session_b,
        snippet_a=snippet_a,
        snippet_b=snippet_b,
        conflict_lines=conflict_lines,
        resolution_hint=hint,
    )


# ---------------------------------------------------------------------------
# Crash bundle export (T585)
# ---------------------------------------------------------------------------


def build_crash_bundle(
    workdir: Path,
    *,
    include_traces: bool = True,
    include_metrics: bool = True,
    max_trace_bytes: int = 50_000,
) -> dict[str, Any]:
    """Build a crash diagnostic bundle for operator export (T585).

    Collects recent traces, metric summaries, and runtime state into a
    single dict suitable for JSON export or TUI display.

    Args:
        workdir: Project root directory.
        include_traces: Whether to include recent trace data.
        include_metrics: Whether to include metric summaries.
        max_trace_bytes: Maximum bytes of trace data to include.

    Returns:
        Dict with ``traces``, ``metrics``, ``runtime``, and ``captured_at``.
    """
    bundle: dict[str, Any] = {
        "captured_at": time.time(),
        "workdir": str(workdir),
        "traces": [],
        "metrics_summary": {},
        "runtime_files": [],
    }

    if include_traces:
        traces_dir = workdir / ".sdd" / "traces"
        if traces_dir.exists():
            trace_files = sorted(traces_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            total_bytes = 0
            for tf in trace_files[:10]:
                if total_bytes >= max_trace_bytes:
                    break
                try:
                    content = tf.read_text(encoding="utf-8", errors="replace")
                    total_bytes += len(content)
                    bundle["traces"].append({"file": tf.name, "content": content[: max_trace_bytes - total_bytes]})
                except OSError:
                    pass

    if include_metrics:
        metrics_dir = workdir / ".sdd" / "metrics"
        if metrics_dir.exists():
            metric_files = list(metrics_dir.glob("*.jsonl"))
            bundle["metrics_summary"] = {
                "file_count": len(metric_files),
                "files": [f.name for f in metric_files[:20]],
            }

    runtime_dir = workdir / ".sdd" / "runtime"
    if runtime_dir.exists():
        bundle["runtime_files"] = [f.name for f in runtime_dir.iterdir() if f.is_file()][:30]

    return bundle
