"""Event-driven trigger manager — evaluates incoming events against user-defined rules.

The TriggerManager is the central coordinator for event-driven agent triggers.
It receives normalized TriggerEvents, matches them against trigger rules loaded
from .sdd/config/triggers.yaml, evaluates conditions (cooldown, dedup, rate
limits), and creates tasks on the task server.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import yaml

from bernstein.core.defaults import TRIGGER
from bernstein.core.models import (
    TriggerConfig,
    TriggerEvent,
    TriggerFireRecord,
    TriggerTaskTemplate,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.task_store import TaskStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety defaults
# ---------------------------------------------------------------------------

_DEFAULT_MAX_TASKS_PER_MINUTE = TRIGGER.max_tasks_per_minute
_DEFAULT_MAX_TASKS_PER_TRIGGER_PER_HOUR = TRIGGER.max_tasks_per_trigger_per_hour
_DEFAULT_EXCLUDE_SENDERS: list[str] = ["bernstein[bot]", "github-actions[bot]"]
_DEFAULT_EXCLUDE_COMMIT_PATTERNS: list[str] = [r"\[bernstein\]"]

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _parse_task_template(raw: dict[str, Any]) -> TriggerTaskTemplate:
    """Parse the ``task`` section of a trigger config."""
    escalation: dict[int, dict[str, str]] = {}
    for k, v in raw.get("model_escalation", {}).items():
        escalation[int(k)] = dict(v)
    return TriggerTaskTemplate(
        title=raw.get("title", "Triggered task"),
        role=raw.get("role", "backend"),
        priority=int(raw.get("priority", 2)),
        scope=raw.get("scope", "small"),
        task_type=raw.get("task_type", "standard"),
        description_template=raw.get("description_template", ""),
        model=raw.get("model"),
        effort=raw.get("effort"),
        model_escalation=escalation,
    )


def load_trigger_configs(path: Path) -> list[TriggerConfig]:
    """Load trigger rules from a YAML config file.

    Args:
        path: Path to ``triggers.yaml``.

    Returns:
        List of parsed TriggerConfig objects.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the config is malformed.
    """
    if not path.exists():
        raise FileNotFoundError(f"Trigger config not found: {path}")
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"Malformed trigger config: {exc}") from exc

    if not isinstance(data, dict) or "triggers" not in data:
        raise ValueError("Trigger config must have a top-level 'triggers' key")

    configs: list[TriggerConfig] = []
    for raw in data["triggers"]:
        if not isinstance(raw, dict) or "name" not in raw or "source" not in raw:
            logger.warning("Skipping malformed trigger entry: %r", raw)
            continue
        task_raw = raw.get("task", {})
        configs.append(
            TriggerConfig(
                name=raw["name"],
                source=raw["source"],
                enabled=raw.get("enabled", True),
                filters=dict(raw.get("filters", {})),
                conditions=dict(raw.get("conditions", {})),
                task=_parse_task_template(task_raw),
                schedule=raw.get("schedule"),
            )
        )
    return configs


# ---------------------------------------------------------------------------
# Filter evaluation
# ---------------------------------------------------------------------------


def _glob_match(pattern: str, value: str) -> bool:
    """Match a value against a glob pattern supporting ``**``.

    ``**`` matches zero or more path segments.  This tries both the
    original pattern and a variant with ``**/`` collapsed so that e.g.
    ``src/**/*.py`` matches both ``src/app.py`` and ``src/sub/app.py``.
    """
    if fnmatch.fnmatch(value, pattern):
        return True
    if "**" in pattern:
        # Try with ** collapsed to match zero intermediate directories
        collapsed = pattern.replace("**/", "")
        if fnmatch.fnmatch(value, collapsed):
            return True
        # Also try PurePath.match for deeper paths
        from pathlib import PurePath

        if PurePath(value).match(pattern):
            return True
    return False


def _glob_match_any(patterns: list[str], values: list[str]) -> bool:
    """Return True if any value matches any glob pattern."""
    return any(_glob_match(p, v) for p in patterns for v in values)


def _matches_filter(event: TriggerEvent, trigger: TriggerConfig) -> bool:
    """Evaluate whether a TriggerEvent passes a trigger's filters."""
    filters = trigger.filters

    # Sender exclusion (applies to all sources)
    exclude_senders = filters.get("exclude_senders", []) + _DEFAULT_EXCLUDE_SENDERS
    if event.sender and event.sender in exclude_senders:
        logger.debug("Trigger %s: sender %r excluded", trigger.name, event.sender)
        return False

    if trigger.source == "github_push":
        # Branch filter
        branches = filters.get("branches", [])
        if branches and event.branch not in branches:
            return False
        # Path filters
        paths = filters.get("paths", [])
        if paths and not _glob_match_any(paths, list(event.changed_files)):
            return False
        exclude_paths = filters.get("exclude_paths", [])
        if exclude_paths and event.changed_files:
            from pathlib import PurePath

            remaining = [f for f in event.changed_files if not any(PurePath(f).match(p) for p in exclude_paths)]
            if not remaining:
                return False
        # Commit pattern exclusion
        exclude_commit_patterns = filters.get("exclude_commit_patterns", _DEFAULT_EXCLUDE_COMMIT_PATTERNS)
        if event.message:
            for pattern in exclude_commit_patterns:
                if re.search(pattern, event.message):
                    logger.debug("Trigger %s: commit message matches exclude pattern %r", trigger.name, pattern)
                    return False

    elif trigger.source == "github_workflow_run":
        conclusion = filters.get("conclusion")
        if conclusion and event.metadata.get("conclusion") != conclusion:
            return False
        workflow_names = filters.get("workflow_names", [])
        if workflow_names and event.metadata.get("workflow_name") not in workflow_names:
            return False
        exclude_workflow_names = filters.get("exclude_workflow_names", [])
        if event.metadata.get("workflow_name") in exclude_workflow_names:
            return False

    elif trigger.source == "slack":
        channels = filters.get("channels", [])
        if channels and event.metadata.get("channel") not in channels:
            return False
        if filters.get("mention_required") and event.message and "@bernstein" not in event.message:
            return False
        msg_pattern = filters.get("message_pattern")
        if msg_pattern and event.message and not re.search(msg_pattern, event.message):
            return False

    elif trigger.source == "file_watch":
        patterns = filters.get("patterns", [])
        if patterns and not _glob_match_any(patterns, list(event.changed_files)):
            return False
        exclude_patterns = filters.get("exclude_patterns", [])
        if exclude_patterns and event.changed_files:
            from pathlib import PurePath

            remaining = [f for f in event.changed_files if not any(PurePath(f).match(p) for p in exclude_patterns)]
            if not remaining:
                return False
        allowed_events = filters.get("events", [])
        if allowed_events and event.metadata.get("event_type") not in allowed_events:
            return False

    elif trigger.source == "webhook":
        path_filter = filters.get("path")
        if path_filter and event.metadata.get("request_path") != path_filter:
            return False
        method_filter = filters.get("method")
        if method_filter and event.metadata.get("request_method") != method_filter:
            return False
        header_filters: dict[str, str] = filters.get("headers", {})
        request_headers: dict[str, str] = event.metadata.get("request_headers", {})
        for key, expected in header_filters.items():
            actual = request_headers.get(key, "")
            if actual != expected:
                return False

    return True


# ---------------------------------------------------------------------------
# Dedup key computation
# ---------------------------------------------------------------------------


def compute_dedup_key(trigger_name: str, event: TriggerEvent) -> str:
    """Compute a deduplication key for a trigger + event pair."""
    # Use SHA-256 of (trigger_name + source + branch/channel/path + sha/timestamp_bucket)
    parts = [trigger_name, event.source]
    if event.branch:
        parts.append(event.branch)
    if event.sha:
        parts.append(event.sha)
    if event.metadata.get("channel"):
        parts.append(event.metadata["channel"])
    if event.metadata.get("request_path"):
        parts.append(event.metadata["request_path"])
    # For sources without a unique key (cron, file_watch), use a 60s bucket
    if event.source in ("cron", "file_watch"):
        bucket = str(int(event.timestamp) // 60)
        parts.append(bucket)
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def _infer_role_from_files(files: list[str]) -> str:
    """Infer task role from changed file paths."""
    for f in files:
        if f.startswith("tests/") or f.startswith("test_"):
            return "qa"
        if f.startswith("docs/") or f.startswith("README"):
            return "docs"
    return "backend"


def render_task_payload(
    trigger: TriggerConfig,
    event: TriggerEvent,
    dedup_key: str,
    retry_count: int = 0,
) -> dict[str, Any]:
    """Render a task creation payload from a trigger template + event.

    Args:
        trigger: The trigger config with task template.
        event: The normalized event.
        dedup_key: Dedup key for audit trail.
        retry_count: Number of prior retries (for model escalation).

    Returns:
        Dict matching ``TaskCreate`` fields.
    """
    template = trigger.task
    sha_short = event.sha[:8] if event.sha else ""
    commit_messages = event.message or ""
    changed_files_str = "\n".join(event.changed_files) if event.changed_files else ""
    changed_count = str(len(event.changed_files))
    message_preview = (event.message or "")[:60]

    # Template variable map
    variables: dict[str, str] = {
        "branch": event.branch or "",
        "sha": event.sha or "",
        "sha_short": sha_short,
        "sender": event.sender or "",
        "repo": event.repo or "",
        "changed_files": changed_files_str,
        "changed_count": changed_count,
        "commit_messages": commit_messages,
        "workflow_name": event.metadata.get("workflow_name", ""),
        "message_text": event.message or "",
        "message_preview": message_preview,
        "channel": event.metadata.get("channel", ""),
        "environment": event.metadata.get("environment", ""),
        "date": time.strftime("%Y-%m-%d"),
        "trigger_name": trigger.name,
    }

    def _interpolate(text: str) -> str:
        result = text
        for key, value in variables.items():
            result = result.replace(f"{{{key}}}", value)
        return result

    # Determine role
    role = template.role
    if role == "auto":
        role = _infer_role_from_files(list(event.changed_files))

    # Model escalation for CI fix triggers
    model = template.model
    effort = template.effort
    if template.model_escalation and retry_count in template.model_escalation:
        esc = template.model_escalation[retry_count]
        model = esc.get("model", model)
        effort = esc.get("effort", effort)

    title = _interpolate(template.title)[:120]
    description = _interpolate(template.description_template)
    # Embed trigger metadata as HTML comment for traceability
    description += f"\n\n<!-- trigger: {trigger.name} source: {event.source} dedup: {dedup_key} -->"

    payload: dict[str, Any] = {
        "title": title,
        "description": description,
        "role": role,
        "priority": template.priority,
        "scope": template.scope,
        "task_type": template.task_type,
    }
    if model:
        payload["model"] = model
    if effort:
        payload["effort"] = effort
    if event.metadata:
        payload["metadata"] = dict(event.metadata)

    return payload


# ---------------------------------------------------------------------------
# TriggerManager
# ---------------------------------------------------------------------------


class TriggerManager:
    """Central coordinator for event-driven triggers.

    Loads trigger rules from ``.sdd/config/triggers.yaml``, evaluates incoming
    events against those rules, enforces conditions (cooldown, dedup, rate
    limits), and creates tasks.
    """

    def __init__(self, sdd_dir: Path, store: TaskStore | None = None) -> None:
        self._sdd_dir = sdd_dir
        self._store = store
        self._config_path = sdd_dir / "config" / "triggers.yaml"
        self._runtime_dir = sdd_dir / "runtime" / "triggers"
        self._runtime_dir.mkdir(parents=True, exist_ok=True)

        self._configs: list[TriggerConfig] = []
        self._config_mtime: float = 0.0

        # In-memory rate limiter: list of fire timestamps in the last 60s
        self._fire_timestamps: list[float] = []
        self._max_tasks_per_minute = _DEFAULT_MAX_TASKS_PER_MINUTE

        # Dedup cache: {dedup_key: expiry_timestamp}
        self._dedup_cache: dict[str, float] = {}

        # Cron state: {trigger_name: last_fire_minute}
        self._cron_state: dict[str, str] = {}

        # Load persisted state
        self._load_dedup_cache()
        self._load_cron_state()

        # Try loading config (graceful if missing)
        self._try_reload_config()

    # -- Config loading & hot-reload ----------------------------------------

    def _try_reload_config(self) -> None:
        """Load or hot-reload trigger configs if the file changed."""
        if not self._config_path.exists():
            self._configs = []
            return
        try:
            mtime = self._config_path.stat().st_mtime
            if mtime != self._config_mtime:
                self._configs = load_trigger_configs(self._config_path)
                self._config_mtime = mtime
                # Read global defaults
                with open(self._config_path) as f:
                    data = yaml.safe_load(f)
                defaults = data.get("defaults", {}) if isinstance(data, dict) else {}
                self._max_tasks_per_minute = int(defaults.get("max_tasks_per_minute", _DEFAULT_MAX_TASKS_PER_MINUTE))
                logger.info("Loaded %d trigger configs from %s", len(self._configs), self._config_path)
        except (ValueError, FileNotFoundError) as exc:
            logger.error("Failed to load trigger config: %s", exc)
            self._configs = []

    @property
    def configs(self) -> list[TriggerConfig]:
        """Return current trigger configs, hot-reloading if file changed."""
        self._try_reload_config()
        return self._configs

    @property
    def is_disabled(self) -> bool:
        """Check if the trigger system is disabled (marker file present)."""
        return (self._runtime_dir / "disabled").exists()

    def disable(self, reason: str) -> None:
        """Disable the trigger system by writing a marker file."""
        (self._runtime_dir / "disabled").write_text(reason)
        logger.error("Trigger system disabled: %s", reason)

    def enable(self) -> None:
        """Re-enable the trigger system by removing the marker file."""
        marker = self._runtime_dir / "disabled"
        if marker.exists():
            marker.unlink()
            logger.info("Trigger system re-enabled")

    # -- Dedup cache --------------------------------------------------------

    def _load_dedup_cache(self) -> None:
        path = self._runtime_dir / "dedup_cache.json"
        if path.exists():
            try:
                with open(path) as f:
                    self._dedup_cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt dedup cache, treating as empty")
                self._dedup_cache = {}

    def _save_dedup_cache(self) -> None:
        # Prune expired entries
        now = time.time()
        self._dedup_cache = {k: v for k, v in self._dedup_cache.items() if v > now}
        path = self._runtime_dir / "dedup_cache.json"
        with open(path, "w") as f:
            json.dump(self._dedup_cache, f)

    def _check_dedup(self, dedup_key: str) -> bool:
        """Return True if the key is a duplicate (should be skipped)."""
        expiry = self._dedup_cache.get(dedup_key)
        return bool(expiry is not None and expiry > time.time())

    def _record_dedup(self, dedup_key: str, ttl_s: int) -> None:
        self._dedup_cache[dedup_key] = time.time() + ttl_s
        self._save_dedup_cache()

    # -- Cron state ---------------------------------------------------------

    def _load_cron_state(self) -> None:
        path = self._runtime_dir / "cron_state.json"
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                self._cron_state = {k: v.get("last_fire_minute", "") for k, v in data.items()}
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt cron state, treating as empty")
                self._cron_state = {}

    def _save_cron_state(self) -> None:
        path = self._runtime_dir / "cron_state.json"
        data = {k: {"last_fire_minute": v, "last_fired": time.time()} for k, v in self._cron_state.items()}
        with open(path, "w") as f:
            json.dump(data, f)

    # -- Fire log -----------------------------------------------------------

    def _record_fire(self, record: TriggerFireRecord) -> None:
        """Append a fire record to the fire log."""
        path = self._runtime_dir / "fire_log.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    def _last_fire_time(self, trigger_name: str) -> float | None:
        """Return the most recent fire timestamp for a trigger, or None."""
        path = self._runtime_dir / "fire_log.jsonl"
        if not path.exists():
            return None
        last: float | None = None
        try:
            for line in path.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("trigger_name") == trigger_name:
                    last = entry.get("fired_at")
        except (json.JSONDecodeError, OSError):
            logger.warning("Error reading fire log")
        return last

    def get_fire_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent fire log entries."""
        path = self._runtime_dir / "fire_log.jsonl"
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            for line in path.read_text().strip().split("\n"):
                if not line:
                    continue
                entries.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            return []
        return entries[-limit:]

    # -- Rate limiting ------------------------------------------------------

    def _check_rate_limit(self) -> bool:
        """Return True if global rate limit is exceeded."""
        now = time.time()
        cutoff = now - 60.0
        self._fire_timestamps = [t for t in self._fire_timestamps if t > cutoff]
        return len(self._fire_timestamps) >= self._max_tasks_per_minute

    def _record_rate(self) -> None:
        self._fire_timestamps.append(time.time())

    # -- Condition evaluation -----------------------------------------------

    def _check_conditions(self, trigger: TriggerConfig, event: TriggerEvent) -> str | None:
        """Check trigger conditions. Returns suppression reason or None if all pass."""
        conditions = trigger.conditions

        # Cooldown
        cooldown_s = conditions.get("cooldown_s", 0)
        if cooldown_s > 0:
            last = self._last_fire_time(trigger.name)
            if last is not None and (time.time() - last) < cooldown_s:
                return f"cooldown (last fired {int(time.time() - last)}s ago, cooldown={cooldown_s}s)"

        # min_commits (push triggers)
        min_commits = conditions.get("min_commits")
        if min_commits is not None:
            commits = event.raw_payload.get("commits", [])
            if len(commits) < min_commits:
                return f"min_commits ({len(commits)} < {min_commits})"

        # max_retries (CI triggers)
        max_retries = conditions.get("max_retries")
        if max_retries is not None and self._store is not None:
            from bernstein.core.models import TaskStatus

            active_statuses = {TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS, TaskStatus.FAILED}
            tasks = self._store.list_tasks()
            title_prefix = trigger.task.title.split("{")[0] if "{" in trigger.task.title else trigger.task.title
            existing = sum(1 for t in tasks if t.title.startswith(title_prefix) and t.status in active_statuses)
            if existing >= max_retries:
                return f"max_retries ({existing}/{max_retries})"

        # skip_if_active (cron triggers)
        if conditions.get("skip_if_active") and self._store is not None:
            from bernstein.core.models import TaskStatus

            active_statuses = {TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS}
            tasks = self._store.list_tasks()
            active = any(
                t for t in tasks if f"<!-- trigger: {trigger.name}" in t.description and t.status in active_statuses
            )
            if active:
                return "skip_if_active (previous task still active)"

        return None

    # -- Main evaluate pipeline ---------------------------------------------

    def evaluate(
        self,
        event: TriggerEvent,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Evaluate an event against all trigger rules.

        This is the main entry point. It runs the full pipeline:
        match → conditions → dedup → render.

        Args:
            event: Normalized trigger event.

        Returns:
            Tuple of (list of TaskCreate payloads, dict of suppressed trigger names → reasons).
        """
        if self.is_disabled:
            return [], {"__system__": "trigger_system_disabled"}

        self._try_reload_config()
        task_payloads: list[dict[str, Any]] = []
        suppressed: dict[str, str] = {}

        # Global rate limit check
        if self._check_rate_limit():
            self.disable(f"Global rate limit exceeded ({self._max_tasks_per_minute} tasks/min)")
            return [], {"__system__": "rate_limit_exceeded"}

        for trigger in self._configs:
            if not trigger.enabled:
                suppressed[trigger.name] = "disabled"
                continue

            # Source must match
            if trigger.source != event.source:
                continue

            # Filter evaluation
            if not _matches_filter(event, trigger):
                suppressed[trigger.name] = "no_filter_match"
                continue

            # Condition evaluation
            reason = self._check_conditions(trigger, event)
            if reason:
                suppressed[trigger.name] = reason
                logger.info("Trigger %s suppressed by %s", trigger.name, reason)
                continue

            # Dedup
            dedup_key = compute_dedup_key(trigger.name, event)
            if self._check_dedup(dedup_key):
                suppressed[trigger.name] = "deduplicated"
                logger.info("Trigger %s deduplicated (key=%s)", trigger.name, dedup_key)
                continue

            # Determine retry count for model escalation
            retry_count = 0
            if trigger.conditions.get("max_retries") and self._store:
                from bernstein.core.models import TaskStatus

                active_statuses = {TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS, TaskStatus.FAILED}
                tasks = self._store.list_tasks()
                title_prefix = trigger.task.title.split("{")[0] if "{" in trigger.task.title else trigger.task.title
                retry_count = sum(1 for t in tasks if t.title.startswith(title_prefix) and t.status in active_statuses)

            # Render task payload
            try:
                payload = render_task_payload(trigger, event, dedup_key, retry_count)
            except Exception as exc:
                logger.error("Template render error for trigger %s: %s", trigger.name, exc)
                suppressed[trigger.name] = f"template_error: {exc}"
                continue

            task_payloads.append(payload)

            # Record dedup and fire
            cooldown_s = trigger.conditions.get("cooldown_s", 300)
            self._record_dedup(dedup_key, max(cooldown_s, 300))
            self._record_rate()

            logger.info("Trigger %s fired for %s event", trigger.name, event.source)

        return task_payloads, suppressed

    def record_fire(self, trigger_name: str, source: str, task_id: str, dedup_key: str, summary: str) -> None:
        """Record a trigger fire after task creation succeeds."""
        record = TriggerFireRecord(
            trigger_name=trigger_name,
            source=source,
            fired_at=time.time(),
            task_id=task_id,
            dedup_key=dedup_key,
            event_summary=summary,
        )
        self._record_fire(record)

    # -- Cron evaluation (called from orchestrator tick) ---------------------

    def evaluate_cron_triggers(self) -> list[TriggerEvent]:
        """Evaluate all cron triggers against the current time.

        Returns a list of synthesized TriggerEvents for cron triggers that
        should fire this minute. Safe to call on every orchestrator tick
        (3s) — uses cron_state to prevent double-firing within the same minute.
        """
        try:
            from croniter import croniter
        except ImportError:
            return []

        self._try_reload_config()
        events: list[TriggerEvent] = []
        now = time.time()
        current_minute = time.strftime("%Y-%m-%dT%H:%M", time.localtime(now))

        for trigger in self._configs:
            if not trigger.enabled or trigger.source != "cron" or not trigger.schedule:
                continue

            # Already fired this minute?
            if self._cron_state.get(trigger.name) == current_minute:
                continue

            try:
                cron = croniter(trigger.schedule, time.localtime(now))
                # Check if the current minute matches the schedule
                prev_fire = cron.get_prev(float)
                # If the previous fire time is within this minute, fire
                prev_minute = time.strftime("%Y-%m-%dT%H:%M", time.localtime(prev_fire))
                if prev_minute != current_minute:
                    continue
            except (ValueError, KeyError) as exc:
                logger.error("Invalid cron expression for trigger %s: %s", trigger.name, exc)
                continue

            event = TriggerEvent(
                source="cron",
                timestamp=now,
                raw_payload={"trigger_name": trigger.name, "schedule": trigger.schedule},
                message=f"Cron trigger: {trigger.name}",
                metadata={"cron_name": trigger.name},
            )
            events.append(event)

            # Mark as fired for this minute
            self._cron_state[trigger.name] = current_minute
            self._save_cron_state()

        return events

    # -- Summary for CLI ----------------------------------------------------

    def list_triggers(self) -> list[dict[str, Any]]:
        """Return a summary of all configured triggers for CLI display."""
        self._try_reload_config()
        result: list[dict[str, Any]] = []
        for trigger in self._configs:
            last_fire = self._last_fire_time(trigger.name)
            result.append(
                {
                    "name": trigger.name,
                    "source": trigger.source,
                    "enabled": trigger.enabled,
                    "schedule": trigger.schedule,
                    "last_fired": last_fire,
                    "filters": trigger.filters,
                }
            )
        return result
